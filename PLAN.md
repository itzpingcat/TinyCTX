# PLAN: Scoped Tool Permissions

**Goal:** Prevent a jailbroken AI from causing destruction when interacting with
untrusted users. Each tool declares a minimum permission threshold (0–100).
Inbound messages are stamped with the triggering sender's permission level. The
tool list sent to the LLM is filtered to only what that user is allowed to call.
If the LLM still tries to invoke a higher-privilege tool (e.g. via a jailbreak),
`execute_tool_call` returns a hard "insufficient permissions" error instead of
running it.

---

## 1. Permission Scale

An integer in the range **0–100** (inclusive). Higher = more trusted.

```
0        unprivileged / no tools
1–24     guest (read-only, safe tools only)
25–49    regular user
50–74    trusted / moderator
75–99    operator / admin
100      full access — CLI always grants this
```

These bands are **conventions** for module authors. The only hard rules are:

- `caller_level >= tool.min_permission` → allowed
- `caller_level <  tool.min_permission` → denied
- CLI is always `100`, unconditionally

---

## 2. Stamp Every Inbound Message

Add one field to `InboundMessage` in `contracts.py`:

```python
@dataclass(frozen=True)
class InboundMessage:
    ...
    permission_level: int = 25   # NEW — 0-100; bridge sets per triggering sender
```

Default `25` = regular user. Bridges override this based on their own
role/config resolution (see §7).

---

## 3. Annotate Tools with a Minimum Threshold

Add `min_permission` to `register_tool` in `utils/tool_handler.py`:

```python
def register_tool(
    self,
    func: Callable,
    name: str | None = None,
    description: str | None = None,
    always_on: bool = False,
    min_permission: int = 25,        # NEW
):
    ...
    self.tools[name] = {
        ...
        'min_permission': min_permission,
    }
```

**Default `25`** — all existing tools keep working without changes.
Module authors raise this for dangerous operations:

| Tool                  | `min_permission` |
|-----------------------|-----------------|
| `tools_search`        | 25              |
| `view` (read-only)    | 25              |
| `web_search`          | 25              |
| `write_file`          | 50              |
| `shell` / `bash`      | 50              |
| `db_query` (writes)   | 75              |
| Config / admin ops    | 100             |

---

## 4. Thread the Level Through the Agent

### 4a. `AgentLoop` holds the current level

```python
class AgentLoop:
    def __init__(self, ..., permission_level: int = 25):
        ...
        self.permission_level = permission_level   # NEW
```

### 4b. `Lane._drain` syncs it from each incoming message

Permission is **re-evaluated every turn** from the live message — no stale state.

```python
async def _drain(self) -> None:
    while True:
        msg = await self.queue.get()
        if msg is not None:
            self.loop.permission_level = msg.permission_level  # NEW
        self.abort_event.clear()
        try:
            async for event in self.loop.run(msg, abort_event=self.abort_event):
                await self.event_handler(event)
        ...
```

---

## 5. Filter Tool Definitions at Assembly Time

In `agent.py` Stage 2 (Context Assembly), replace:

```python
tools = self.tool_handler.get_tool_definitions() or None
```

with:

```python
tools = self.tool_handler.get_tool_definitions(
    caller_level=self.permission_level
) or None
```

In `ToolCallHandler.get_tool_definitions`:

```python
def get_tool_definitions(self, caller_level: int = 100) -> list[dict]:
    definitions = []
    for name in self.enabled:
        tool = self.tools.get(name)
        if tool is None:
            continue
        if caller_level < tool['min_permission']:
            continue   # silently excluded — LLM never sees this tool
        definitions.append(...)
    return definitions
```

The LLM **never sees** tools the caller doesn't have permission for.

---

## 6. Enforce at Execution Time (Defence in Depth)

Even if a jailbreak causes the LLM to hallucinate a call to a filtered-out
tool, `execute_tool_call` blocks it unconditionally:

```python
async def execute_tool_call(self, tool_call, caller_level: int = 100) -> dict:
    ...
    tool = self.tools.get(function_name)

    if tool is None or function_name not in self.enabled:
        return {'error': "Tool not found or not enabled", 'success': False, ...}

    # NEW — permission guard
    if caller_level < tool['min_permission']:
        return {
            'tool_call_id': tool_call_id,
            'error': (
                f"[PERMISSION DENIED] '{function_name}' requires permission "
                f">= {tool['min_permission']}; caller has {caller_level}."
            ),
            'success': False,
        }
    ...
```

`AgentLoop._execute_tool` passes the level through:

```python
result = await self.tool_handler.execute_tool_call(
    proxy, caller_level=self.permission_level   # NEW
)
```

---

## 7. Bridge Responsibilities

Each bridge resolves the **triggering sender's** level and stamps it on
`InboundMessage` before calling `router.push()`.

For group channels (Discord, Matrix) this is the person who sent the trigger
message. `GroupLane` already routes the trigger author's `InboundMessage` to
`Lane.enqueue` unchanged — no changes needed to `GroupLane` itself.

Permission config lives **under each bridge's existing block** in `config.yaml`,
accessed via `BridgeConfig.options` (already supports arbitrary keys via
`__getattr__`). No new top-level config key is needed.

### CLI bridge

Always `100`. No config needed.

```python
inbound = InboundMessage(..., permission_level=100)
```

### Discord bridge

```yaml
bridges:
  discord:
    enabled: true
    token: "..."
    default_permission: 25
    admin_users: [123456789012345678]   # these user IDs always get level 100
    role_permissions:
      # Keys are Discord role IDs (integers), NOT role names.
      # Role names can be renamed by anyone with Manage Roles; IDs are permanent.
      # Right-click a role in Discord (Developer Mode on) → Copy Role ID.
      # IMPORTANT: roles are per-server. A role ID from server A is not
      # present in server B — a user in server B without a matching role
      # will fall back to default_permission regardless of their status elsewhere.
      123456789012345678: 100   # Admin role in server A
      234567890123456789: 75    # Moderator role in server A
      345678901234567890: 50    # Trusted role in server A
```

Resolution order (highest wins):
1. If sender's user ID is in `admin_users` → unconditional `100`.
2. Iterate sender's guild roles highest-position first; return first mapped level.
3. Fall back to `default_permission`.

**Role IDs are server-scoped.** A role that exists in server A has no
presence in server B — `member.roles` only contains roles from the guild the
message was sent in. If you want elevated permissions across multiple servers,
you must either add the relevant role IDs from each server to `role_permissions`,
or add the user's ID to `admin_users` (which is server-agnostic).

```python
def _resolve_permission_level(self, member_roles: list | None) -> int:
    role_map = self._opts.get("role_permissions", {})
    default  = int(self._opts.get("default_permission", 25))
    # admin_users always get 100, regardless of roles or server.
    # (Checked at call site before this function is called.)
    if not member_roles or not role_map:
        return default
    # Normalise keys to int so YAML integer keys and string keys both work.
    int_map = {int(k): int(v) for k, v in role_map.items()}
    for role in sorted(member_roles, key=lambda r: r.position, reverse=True):
        if role.id in int_map:
            return int_map[role.id]
    return default
```

The `admin_users` check is applied in `_on_message` before calling
`_resolve_permission_level`, granting level `100` unconditionally:

```python
if message.author.id in self._admin_users:
    permission_level = 100
else:
    permission_level = self._resolve_permission_level(
        getattr(message.author, "roles", None)
    )
```

### Matrix bridge

```yaml
bridges:
  matrix:
    enabled: true
    homeserver: "..."
    default_permission: 25
    power_level_map:
      100: 100
      50:  50
      0:   25
```

Resolution: look up sender's room power level, map through `power_level_map`
(exact match, then nearest lower key), fall back to `default_permission`.

---

## 8. Files Changed

| File | Change |
|------|--------|
| `contracts.py` | Add `permission_level: int = 25` to `InboundMessage` |
| `utils/tool_handler.py` | `register_tool` gains `min_permission`; `get_tool_definitions(caller_level)`; `execute_tool_call(caller_level)` |
| `agent.py` | `AgentLoop` gets `permission_level` attr; passes `caller_level` in Stage 2 and `_execute_tool` |
| `router.py` | `Lane._drain` syncs `loop.permission_level` from each message |
| `bridges/cli/` | Stamp `permission_level=100` unconditionally |
| `bridges/discord/` | Read `role_permissions` + `default_permission` + `admin_users` from options, resolve per sender; `admin_users` → unconditional 100 |
| `bridges/matrix/` | Read `power_level_map` + `default_permission` from options, resolve per sender |
| `example.config.yaml` | Document the new per-bridge permission keys |
| `modules/*/` | Annotate dangerous `register_tool(...)` calls with `min_permission=N` |

---

## 9. What This Prevents

| Attack | Blocked by |
|--------|-----------|
| Jailbreak tries to call `shell` | Tool absent from list sent to LLM (§5) |
| LLM hallucinates a filtered tool name | Execution-time guard returns hard denial (§6) |
| `tools_search` "unlocks" a high-priv tool | `get_tool_definitions` still filters by level after enabling — enabling ≠ visible |
| Privilege escalation mid-session | Level re-read from the message on every turn (§4b) |
| Group channel: low-trust user triggers bot | Level comes from the trigger author, not the channel |

---

## 10. Known Footguns

| Pitfall | Explanation |
|---------|-------------|
| Using role names as config keys | Role names can be changed by anyone with Manage Roles. Always use role IDs. |
| Expecting cross-server roles to work | Discord roles are per-guild. A role from server A is invisible in server B. Use `admin_users` for cross-server elevated trust. |
| Assuming `admin_users` grants shell | `admin_users` is used for `/reset` auth AND for permission resolution (grants level 100). Both must be wired — check your bridge code. |
| `_gp_replace_text` drops fields | Any helper that reconstructs `InboundMessage` must copy **all** fields, including `permission_level`. Missing one silently resets it to the default (25). |

---

## 11. Non-Goals

- Per-argument sandboxing (allow `shell` but only `ls`) — separate concern.
- Audit logging of denied calls — trivial one-liner in §6, not in scope here.
- Runtime role changes — bridges re-resolve on every message naturally.
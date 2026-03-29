# TinyCTX Tree Refactor — Planning Log

## Goal

Replace the flat `HistoryEntry` list + `SessionKey → Lane` model with a
conversation tree. Every message is a node with a `parent_id`. Bridges hold
a cursor (a `node_id`) instead of a session key. No separate branch metadata.

This log records decisions and planned changes. Nothing is implemented yet.

---

## Decisions Made

### Tree structure
- A **node** is a single message. Every node has a `parent_id` (None for root).
- A **branch** is just a path through the tree — no type system, no metadata.
- A **cursor** is a `node_id` pointing at the current tail of a branch.
- Runtime behavior (interactive vs background) is determined by whether a
  bridge cursor is pointing at a branch — not a property of the branch itself.
- There is **one global root node** for the entire tree. All bridges share it.
  Bridges can and should be able to walk into each other's branches.

### Storage
- Tree stored in **`agent.db`** (SQLite) in the workspace root.
- Single `nodes` table — one row per node.
- Old `sessions/<key>/<N>.json` files: **nuked, not migrated.** All existing
  sessions are test data. Durable info lives in `AGENTS.md`, `MEMORY.md` etc.
- Writes are immediate — every `context.add()` call writes to DB at node
  creation time. No batching, no end-of-turn flush.

### `SessionKey` is eliminated
- Bridges no longer construct a `SessionKey`.
- Instead, each bridge instance holds a **cursor** — a `node_id` string.
- The cursor is persisted by the bridge and passed into `InboundMessage`
  as `tail_node_id`.
- The router looks up `tail_node_id` in the tree, appends the new user node
  as a child, runs the agent, advances the cursor.

### How bridges get their initial node_id
- **Fresh start**: bridge reads the global root node_id from `agent.db`.
  If the DB is empty, one root node is created at startup (by the tree layer,
  not by any bridge). The bridge then creates its first child of root and
  persists that as its cursor.
- **Resuming**: bridge reads its persisted `node_id`, verifies it exists in
  the tree, attaches.
- **Explicit attach**: bridge is given a `node_id` via config, env var, or
  slash command (`/branch switch <id>`).

### Per-bridge cursor storage
- **CLI**: `workspace/cursors/cli` (plain text, one node_id)
- **Discord**: `workspace/cursors/discord.json` keyed by channel_id
- **Matrix**: `workspace/cursors/matrix.json` keyed by room_id
- **Gateway**: maps URL `session_id` → `node_id` in `workspace/cursors/gateway.json`
- **Cron**: each job config carries its own cursor node_id, persisted in `CRON.json`
- **Heartbeat**: cursor stored in `workspace/cursors/heartbeat` (plain text, one node_id)

### Queue / Lane (deferred)
- `Lane` and `_SessionRouter` are **not touched in this phase**.
- The queue refactor is deferred.
- For now the tree layer sits underneath the existing lane model.
  Lanes are keyed by cursor `node_id` instead of `SessionKey` — minimal change.

### Cross-branch primitives (deferred to Phase 3)
- `spawn_branch`, `send_to_branch`, `wait_for_branch` — not part of Phase 1 or 2.

### GroupPolicy
- Bridges pass `group_policy` in `InboundMessage` same as today.
- The router constructs a `GroupLane` wrapper if needed, same logic as now,
  but keyed by `node_id` instead of `SessionKey`.

---

## Schema — `agent.db`

```sql
CREATE TABLE nodes (
    id           TEXT PRIMARY KEY,  -- uuid
    parent_id    TEXT,              -- NULL for global root
    role         TEXT NOT NULL,     -- user | assistant | system | tool
    content      TEXT NOT NULL,     -- JSON if list (attachment blocks), else plain str
    created_at   REAL NOT NULL,     -- unix timestamp
    tool_calls   TEXT,              -- JSON or NULL
    tool_call_id TEXT,              -- for tool result nodes
    author_id    TEXT,              -- group chat sender, NULL otherwise
    FOREIGN KEY (parent_id) REFERENCES nodes(id)
);

CREATE INDEX idx_nodes_parent ON nodes(parent_id);
```

One global root node is inserted at DB creation time:
```
id=<uuid>, parent_id=NULL, role="system", content="", created_at=<now>
```

---

## Phase 1 — Tree layer + storage (`db.py`, `context.py`, `contracts.py`)

### New: `db.py`

Owns the SQLite connection and all node I/O. No agent/router/bridge imports.

```python
class ConversationDB:
    def __init__(self, path: Path): ...
    def ensure_schema(self): ...           # CREATE TABLE IF NOT EXISTS + root node
    def add_node(parent_id, role, content, **kwargs) -> Node: ...
    def get_node(node_id) -> Node | None: ...
    def get_ancestors(node_id) -> list[Node]: ...  # root → node order
    def get_children(node_id) -> list[Node]: ...
    def get_root() -> Node: ...
```

`get_ancestors` is the hot path — called on every context assembly.
Implemented via recursive CTE:

```sql
WITH RECURSIVE anc AS (
    SELECT * FROM nodes WHERE id = ?
    UNION ALL
    SELECT n.* FROM nodes n JOIN anc a ON n.id = a.parent_id
)
SELECT * FROM anc;
```

Then reversed in Python to get root→node order.

### `contracts.py` changes (Phase 1, minimal)

`InboundMessage`:
- Add `tail_node_id: str | None = None`
- Keep `session_key` for now (bridges not yet migrated)

No other changes in Phase 1.

### `context.py` changes

`HistoryEntry`:
- Add `parent_id: str | None = None`
- All other fields unchanged

`Context`:
- Add `_db: ConversationDB` (injected at construction)
- Add `_tail_node_id: str | None`
- Add `set_tail(node_id)` — called by router/agent to point context at a branch
- Modify `add()` — writes node to DB immediately, advances `_tail_node_id`
- Modify `assemble()` — calls `_db.get_ancestors(tail_node_id)`, converts to
  `HistoryEntry` list, applies existing token-limit trimming logic
- Drop `_history: list[HistoryEntry]` — replaced by DB-backed assembly
- Existing `edit()`, `delete()`, `strip_tool_calls()` — write through to DB

---

## Phase 1 — `agent.py` changes

### Deleted entirely
- `_flush_history()` — gone. DB writes are immediate at `context.add()` time.
  No end-of-turn batch write. The `await self._flush_history()` calls at the
  end of `run()` and on LLM error are simply removed.
- `_restore_history()` — gone. On init, `Context` reconstructs history by
  walking the DB from `tail_node_id`. No JSON file to read.
- `_load_latest_version()` — gone. No session versioning concept.
- `next_session()` — gone. Replaced by branching (future phase).
- `_session_version` counter — gone.

### `reset()` — gutted
- Currently: clears context + writes empty JSON to disk.
- New: calls `context.clear()` only. The tree in `agent.db` is never mutated
  by reset — resetting just moves the cursor, it doesn't delete nodes.
  (Cursor movement is the bridge's responsibility, not the agent's.)

### Signature changes
- `__init__(self, session_key, config, ...)` → `__init__(self, tail_node_id, config, ...)`
- `self.session_key` → `self.tail_node_id`
- The `ev` dict inside `run()` that spreads into every `AgentEvent`:
  `session_key=self.session_key` → `tail_node_id=self.tail_node_id`

### Stage 1 (Intake) — call site unchanged
- `self.context.add(HistoryEntry.user(content))` stays the same.
- The write-through to DB and tail advancement happen inside `Context.add()`.
  The agent doesn't see this change.

### Stage 2 (Context Assembly) — call site unchanged
- `self.context.assemble()` called identically.
- Internally walks `agent.db` via `get_ancestors(tail_node_id)` instead of
  slicing `_history`. Invisible to the agent.

### Everything else — untouched
- Inference cycles, tool execution, streaming, abort, fallback chain: no changes.

---

## Phase 2 — Router + Bridge cursor migration

### `router.py`

- `Lane` keyed by `node_id` (cursor) instead of `SessionKey`
- `_SessionRouter._lanes` becomes `dict[str, Lane | GroupLane]` (str = node_id)
- `route(msg)` uses `msg.tail_node_id` as key
- `GroupLane` logic preserved — activation/buffering unchanged
- `abort_generation(node_id)`, `reset_session(node_id)` updated signatures
- `Router.push(msg)` routes by `msg.tail_node_id`
- Event dispatch table: `cursor_node_id → handler` instead of `SessionKey → handler`

### `contracts.py`

- Remove `SessionKey`, `ChatType`
- `Platform` kept — bridges still have platform identity for event dispatch
- `InboundMessage`: remove `session_key`, promote `tail_node_id` to required
- `AgentEvent` base: replace `session_key: SessionKey` with `tail_node_id: str`
- `UserIdentity` unchanged
- `GroupPolicy`, `ActivationMode` unchanged

### `agent.py`

- `Lane` passes `tail_node_id` to `AgentLoop.__init__` instead of `session_key`
- All logging that referenced `session_key` uses `tail_node_id`

### Bridges

Each bridge:
1. On startup, load cursor from its persisted location
2. If no cursor: attach to global root, create first child node, persist node_id
3. Build `InboundMessage(tail_node_id=cursor, ...)` instead of `session_key`
4. After agent responds, advance cursor to new tail node_id returned by router

Bridge files:
- `bridges/cli/__main__.py` → `workspace/cursors/cli`
- `bridges/discord/__main__.py` → `workspace/cursors/discord.json` (by channel_id)
- `bridges/matrix/__main__.py` → `workspace/cursors/matrix.json` (by room_id)
- `gateway/__main__.py` → `workspace/cursors/gateway.json` (by session_id)

### `modules/cron/__main__.py`

Currently builds `SessionKey.dm(f"cron-{job.id}")` per job and pushes via
`gateway.push(InboundMessage(session_key=...))`.

Changes:
- Each `CronJob` dataclass gains a `cursor_node_id: str | None` field.
- On first run of a job: create a child node off global root, store the
  returned node_id as `cursor_node_id` in `CRON.json`.
- On subsequent runs: build `InboundMessage(tail_node_id=job.cursor_node_id)`
  and push via gateway as before — routing now uses node_id instead of
  session_key.
- After each run: advance `cursor_node_id` to the new tail node_id.
- `reset_after_run`: instead of `gateway.reset_session(job_session)`, rewind
  `cursor_node_id` back to the job's root node (the one created on first run).
  Tree is never mutated — cursor just rewinds.
- Remove `SessionKey`, `ChatType` imports. Remove `_CRON_USER_ID`,
  `_CRON_SESSION` constants. `_CRON_AUTHOR` and `_CRON_PLATFORM` kept —
  still needed for event dispatch handler registration.

### `modules/heartbeat/__main__.py`

Currently constructs `_HEARTBEAT_SESSION = SessionKey(...)` and builds
`InboundMessage(session_key=session_key, ...)` per tick. Also references
`agent.session_key` to resolve the `"main"` session config.

Changes:
- Replace `_HEARTBEAT_SESSION` constant with a cursor node_id, persisted at
  `workspace/cursors/heartbeat`. Created as a child of global root on first
  run, same pattern as other bridges.
- `_resolve_session(session_cfg, main_key)` is removed entirely. "main" now
  means the agent's current `tail_node_id`. Other values are treated as
  literal node_ids. Config key renamed from `session` to `tail_node_id` (or
  just resolved at startup from config).
- `_run_turn()` builds `InboundMessage(tail_node_id=cursor, ...)` instead of
  `InboundMessage(session_key=..., ...)`.
- Cursor is advanced after each tick the same way bridges advance theirs.
- Remove `SessionKey`, `ChatType` imports and all references to `agent.session_key`.
- Everything else (tick logic, continuation loop, active hours, `_parse_reply`,
  `_emit_alert`, `_patch_reset`) — unchanged.

---

## Phase 3 — Background branches + memory consolidation

### Prerequisites
- Phase 1 and 2 complete.
- `spawn_branch` primitive implemented (see below).

### New primitive: `spawn_branch`

Added to `db.py` / exposed via `Context`:

```python
def spawn_branch(parent_node_id: str, opening_content: str, role: str = "user") -> str:
    """
    Create a child node off parent_node_id. Returns the new node's id.
    The caller is responsible for pointing an AgentLoop at the returned node_id.
    """
```

This is the only new primitive needed. `send_to_branch` and `wait_for_branch`
are deferred further — they require the queue refactor.

### Background AgentLoop

A background branch is just an `AgentLoop` with no bridge cursor pointing at
it. It runs in a detached asyncio task. The runtime spawns it, hands it a
`tail_node_id`, and lets it run to completion unobserved.

```python
async def _run_background(tail_node_id: str, config: Config) -> None:
    loop = AgentLoop(tail_node_id=tail_node_id, config=config)
    async for _ in loop.run(msg=None):  # synthetic turn — no user message
        pass  # events discarded
```

No event handler registered. No bridge cursor. The loop runs, writes nodes
to `agent.db`, and exits.

### Memory consolidation flow

Trigger: end of an interactive turn (after `AgentTextFinal` is yielded).
Location: `agent.py`, after the main `run()` loop, before returning.

```
current tail
     │
     └── [spawn_branch] "consolidate memory from this conversation"
              │
              └── background AgentLoop runs:
                    - walks its own ancestor chain for context
                    - calls memory write tools (filesystem, etc.)
                    - exits when done
```

The user's cursor is never moved. The background branch is a dead-end from
the user's perspective unless they explicitly `/branch switch` to it.

### Memory module changes (`modules/memory/__main__.py`)

**Phase 1 fix (small, needed now):**
- `_nudge_hook` currently does `ctx.dialogue.append(HistoryEntry.user(msg))`.
- Change to `ctx.add(HistoryEntry.user(msg))` so the write goes through to DB
  and advances the tail. One line change.

**Phase 3 change (background branch):**
- Remove the inline nudge that injects a summarization message into the
  current conversation.
- Instead, at nudge threshold, call `spawn_branch` with the summarization
  instruction as the opening node, then fire `_run_background` as a detached
  asyncio task.
- The current conversation continues uninterrupted. Memory consolidation
  happens in the background branch.
- The `_nudge_hook` becomes a branch-spawning hook rather than a
  context-injecting hook.

**All other memory module hooks/tools — unchanged:**
- Static prompt providers (SOUL, AGENTS, MEMORY): no changes.
- `_pre_assemble_async` search hook: no changes.
- `memory_search` tool: no changes.
- Auto-inject prompt provider: no changes.

---

## Files to touch

### Phase 1

| File | Change |
|------|--------|
| `db.py` (new) | `ConversationDB` — schema, CRUD, ancestor walk via recursive CTE |
| `contracts.py` | Add `tail_node_id: str | None` to `InboundMessage` |
| `context.py` | Add `parent_id` to `HistoryEntry`; inject `_db`; rewrite `assemble()` and `add()`; drop `_history` list and session JSON logic |
| `agent.py` | Delete `_flush_history`, `_restore_history`, `_load_latest_version`, `next_session`; gut `reset()`; `session_key` → `tail_node_id`; remove `await _flush_history()` call sites |
| `modules/memory/__main__.py` | `ctx.dialogue.append(...)` → `ctx.add(...)` in `_nudge_hook` |
| `sessions/` (dir) | Delete entirely |

### Phase 2

| File | Change |
|------|--------|
| `contracts.py` | Remove `SessionKey`, `ChatType`; promote `tail_node_id` to required; update `AgentEvent` base |
| `router.py` | Key lanes by `node_id`; update dispatch table |
| `bridges/cli/__main__.py` | Cursor persistence, remove `SessionKey` |
| `bridges/discord/__main__.py` | Per-channel cursor persistence |
| `bridges/matrix/__main__.py` | Per-room cursor persistence |
| `gateway/__main__.py` | `session_id → node_id` mapping |
| `modules/cron/__main__.py` | Add `cursor_node_id` to `CronJob`; build `InboundMessage(tail_node_id=...)`; per-job cursor init + advance; `reset_after_run` rewinds cursor; remove `SessionKey`/`ChatType` imports |
| `modules/heartbeat/__main__.py` | Replace `_HEARTBEAT_SESSION` with cursor node_id at `workspace/cursors/heartbeat`; remove `_resolve_session`; build `InboundMessage(tail_node_id=...)`; advance cursor per tick; remove `SessionKey`/`ChatType` imports |

### Phase 3

| File | Change |
|------|--------|
| `db.py` | Add `spawn_branch()` |
| `context.py` | Expose `spawn_branch()` via `Context` |
| `agent.py` | Add `_run_background()` helper; trigger background branch at turn end |
| `modules/memory/__main__.py` | Replace inline nudge with `spawn_branch` + detached task |

### Not touched (any phase)

| File | Reason |
|------|--------|
| `ai.py` | No session concepts |
| `config/` | No changes needed |
| `utils/` | No session concepts |
| `modules/web`, `modules/filesystem`, `modules/ctx_tools`, `modules/skills`, `modules/mcp` | Hook contract `async def hook(ctx)` unchanged across all phases |

---

## Open Questions (all resolved)

| # | Question | Decision |
|---|----------|----------|
| 1 | Global root vs per-bridge roots? | **One global root.** All bridges share it and can walk into each other's branches. |
| 2 | Migrate existing session JSON? | **Nuke it.** No migration. Durable info is in workspace markdown files. |
| 3 | GroupPolicy on cursors? | Pass in `InboundMessage` same as today. Router constructs GroupLane wrapper as needed. |
| 4 | Storage format? | **SQLite (`agent.db`)** in workspace root. Single `nodes` table. |
| 5 | When do DB writes happen? | **Immediately at `context.add()` time.** No end-of-turn flush. `_flush_history` deleted. |
| 6 | Agent loop scope of change? | Mostly mechanical: delete session file methods, find-replace `session_key` → `tail_node_id`. Core loop stages unchanged. |
| 7 | Other modules need changes? | **Yes — cron and heartbeat do.** Both build `SessionKey` and `InboundMessage(session_key=...)` directly. Memory needs one-line fix in Phase 1, nudge refactor in Phase 3. `web`, `filesystem`, `ctx_tools`, `skills`, `mcp` are genuinely untouched. |

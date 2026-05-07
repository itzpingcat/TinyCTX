# TinyCTX Architecture Redesign — PLAN.md

## Problems Being Solved

1. **GroupLane / multi-user handling is hacky.** Messages get collapsed into a
   single buffered node. Attribution is glued on after the fact. The buffer is
   invisible state, lossy across restarts, and makes nodes ambiguous.

2. **Bridge inconsistency.** CLI speaks HTTP; Discord/Matrix call `router.push()`
   in-process. Every bridge should be identical in shape.

3. **AgentLoop / Lane / Router are stateful and tangled.** A `Lane` owns a live
   `AgentLoop` which owns `Context`, tool handler, command registry wiring, DB
   connection, cursor files, background hooks — all per-session. No clean seam
   between "one turn of reasoning" and "the long-lived session object".

4. **Session state lives in memory.** `agent.context.state` holds platform,
   author, channel, enabled tools, and other per-session data — all lost on
   restart, not reproducible from the DB, not branchable.

---

## Target Architecture

```
[Bridge] ──HTTP──> [Gateway] ──> [Runtime.push(InboundMessage)]
                                       │
                            save attachments to disk
                            compute state delta vs previous node
                            persist user node + delta to DB
                                       │
                              msg.trigger == True?
                                  │           │
                                 Yes          No
                                  │           └─> return (accepted)
                                  │
                    at_capacity? reject : asyncio.create_task(_process(msg))
                                  │
                    AgentCycle.run() (concurrent, fire-and-forget)
                                  │
                     Context walks ancestors, replays state deltas
                     writes assistant/tool nodes to DB
                     (concurrent cycles on same branch = natural fork)
                                  │
                          yields AgentEvents
                                  │
                         SSE fanout → gateway → bridges
                                  │
                       task exits and is garbage collected
```

---

## Key Design Decisions

### Task pool, not a worker queue

Each `InboundMessage` with `trigger=True` spawns an `asyncio.Task` via
`asyncio.create_task()`. Tasks run concurrently, results arrive as they complete,
no ordering guarantee across branches. Tasks are fire-and-forget — they exit when
the cycle finishes and are garbage collected.

Concurrency is capped by a semaphore (`max_workers` in config). At capacity,
`push()` returns `False` and the gateway sends 429. No idle workers.

### Concurrent cycles on the same branch = natural fork

Two cycles triggered simultaneously on the same branch tail do not race
destructively — they branch. Each cycle writes its assistant response as a child
of its own `tail_node_id`, producing two valid subtrees. The graph structure makes
concurrent processing safe without any per-branch locking. This is correct
behaviour, not a bug.

### Everything is a node. Nothing is buffered.

Every inbound message — DM or group, trigger or non-trigger — is persisted as a
node immediately upon receipt. The buffer is the tree. Group conversations produce
one node per message with `author_id` and `author_name` set. No `GroupLane`, no
in-memory buffer, no multi-message nodes.

### State deltas on nodes

Session state (platform, author, channel, enabled tools, etc.) is stored as a JSON
delta on the node that caused the change. `Context` walks the full ancestor chain
(not just the dialogue window) to replay deltas and reconstruct current state.

```
root
  └─ [user] "hey"   state_delta: {platform:"discord", author_id:"u123", author_name:"Kamie", enabled_tools:["web","filesystem"]}
       └─ [assistant] "hi"
            └─ [user] "switch to matrix"   state_delta: {platform:"matrix"}
                 └─ [user] "disable web"   state_delta: {enabled_tools:["filesystem"]}
```

State at any node = merge of all ancestor deltas in root→node order. Only changes
are stored — a delta contains only the keys that changed. Reading a full state
snapshot stored at a checkpoint node is identical in format to reading a delta,
so the replay logic handles both transparently.

**Caution: state deltas may be outside the context window.**
`assemble()` trims old dialogue turns when the token budget is exceeded, but state
deltas must always be fully replayed regardless of trimming. `_load_state_from_db()`
must walk the *complete* ancestor chain for delta replay, independently of however
many dialogue turns `assemble()` decides to include. A session where the platform
was set 50 turns ago must still know its platform.

**Future optimisation: checkpoint snapshots.**
For very long trees, store a full state snapshot as a `state_delta` every N nodes
(e.g. every 20). The replay walk can stop at the most recent full snapshot rather
than walking to root. No changes to reading logic — a full snapshot is just a delta
that happens to contain all keys. This can be added later without any schema change.

**What lives in state deltas:**
- `platform` — bridge platform identifier
- `author_id`, `author_name` — who sent the triggering message
- `server_name`, `channel_name` — guild/channel identity
- `enabled_tools` — list of tool names currently active (replaces runtime mutable state)
- `permission_level` — effective permission for this branch

**What does NOT live in state deltas:**
- Token counts, budget flags — ephemeral, computed per-cycle in `context.state`
- Anything that changes every turn — keep that in-memory on `Context.state` as before
- `activation_mode` — deleted entirely, bridges handle trigger detection locally

### `enabled_tools` in state deltas

Tool enabling/disabling is now durable and branchable. When a user or agent changes
the active tool set, `Runtime.push()` writes the new list as a delta on the user
node. Branching from any point inherits all prior tool state. Rewinding the cursor
rewinds tool state too. `ToolCallHandler.get_tool_definitions()` reads
`enabled_tools` from the assembled state rather than from a mutable runtime field.

### `Context.assemble()` formats group history

If the ancestor chain contains user turns with more than one distinct non-None
`author_id`, `assemble()` prepends `[author_name]: ` to each user turn before
rendering. Replaces all GroupLane buffer formatting.

### `trigger: bool` on `InboundMessage` — bridges decide

```python
@dataclass(frozen=True)
class InboundMessage:
    ...
    trigger: bool = True
```

The bridge sets `trigger`. Trigger detection (mention check, prefix check) is
bridge-local logic. `GroupPolicy`, `ActivationMode`, and `GroupLane` are deleted.

### Attachments: saved to disk by `Runtime.push()`, read by `Context`

`attachments.py` already writes bytes to `workspace/uploads/` (SHA-256 dedup).
`Runtime.push()` calls `save_upload()` and stores paths in the DB node
(`attachment_paths` column, JSON list). `Context._load_from_db()` re-hydrates
`Attachment` objects from disk and calls `build_content_blocks()`. Raw bytes never
reach `AgentCycle`.

### `AgentCycle` — sealed, stateless, no `Runtime` reference

```python
@dataclass
class AgentCycle:
    tail_node_id:     str
    db:               ConversationDB
    models:           dict[str, LLM]
    tool_handler:     ToolCallHandler
    config:           Config
    abort_event:      asyncio.Event
    permission_level: int
    hooks:            CycleHooks
    message_id:       str = "synthetic"
    trace_id:         str = field(default_factory=lambda: str(uuid.uuid4()))
```

No reference to `Runtime`. Cannot call `runtime.push()` or touch module state.
`Runtime` constructs it from its own fields inside `_process()`.

`hooks` is explicit so background cycles can receive a stripped hook set
(empty `post_turn`) to prevent recursive chaining.

### Background branches: gone as a special concept

A background branch is just another `InboundMessage` with `trigger=True` pointing
at a branch node, pushed via `runtime.push()`. No `run_background()`, no
`is_subagent`, no scattered `asyncio.create_task` in module code. The knowledge
module's post-turn hook becomes:

```python
async def _post_turn_hook(tail_node_id: str, runtime: Runtime) -> None:
    opening = runtime.db.add_node(parent_id=tail_node_id, ...)
    await runtime.push(InboundMessage(tail_node_id=opening.id, trigger=True, ...))
```

Background cycles are given `CycleHooks(post_turn=[])`.

### All bridges are HTTP-only

Discord and Matrix bridges drop `router.push()` and gain an HTTP client identical
to the CLI bridge. `run(gw: Router)` becomes `run()`. The gateway is the only
consumer of `Runtime`. In-process event delivery is removed.

---

## What Changes

### `contracts.py`

- Add `trigger: bool = True` to `InboundMessage`.
- Remove `GroupPolicy`, `ActivationMode`.
- Remove `lane_node_id` from `_AgentEventBase`.
- Add `Platform.SYSTEM`.

### `db.py`

- Add `author_name TEXT` column.
- Add `attachment_paths TEXT` column (JSON list of paths, nullable).
- Add `state_delta TEXT` column (JSON object of changed keys, nullable).
- All added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `ensure_schema()`
  for compatibility with existing DBs.
- Update `add_node()` signature and `Node` dataclass accordingly.
- Add `get_ancestors_full()` — like `get_ancestors()` but always walks to root,
  used exclusively for state delta replay (not affected by context trimming).

### `context.py`

- `_load_state_from_db()` — new method. Walks the **complete** ancestor chain via
  `db.get_ancestors_full()`, replays `state_delta` objects in root→tip order,
  returns merged state dict. Called at the start of `assemble()` independently of
  dialogue loading. State is always fully reconstructed regardless of how many
  dialogue turns are trimmed by the token budget.
- `_load_from_db()` reads `author_name` and `attachment_paths`. Re-hydrates
  `Attachment` objects from disk paths; calls `build_content_blocks()`.
- `assemble()` detects multi-author branches and prepends `[Name]: ` to user turns.
- `context.state` retains its role for ephemeral per-cycle data (token counts,
  budget flags) but is no longer the home for durable session identity.

### `agent.py` → `cycle.py`

- `AgentLoop` → `AgentCycle`.
- `CycleHooks` dataclass defined here.
- 6-stage loop moves verbatim; attributes sourced from cycle fields.
- `run_background()`, `is_subagent`, background hook registry deleted.
- `agent.py` kept as stub raising `RuntimeError`.

### `router.py` → deleted

Stub raising `RuntimeError` left temporarily.

### `runtime.py` (new)

Owns DB, models, tool_handler, commands, semaphore, SSE handlers, module loading,
module_env. `push()` computes state delta, persists node, spawns task.
`_process()` acquires semaphore, constructs `AgentCycle`, runs it, exits.

Modules register via `register(runtime: Runtime)`. `Runtime` exposes:
`runtime.tool_handler`, `runtime.commands`, `runtime.config`, `runtime.db`,
`runtime.models`, `runtime.module_env`, `runtime.register_background_hook(fn)`,
`runtime.register_pre_assemble_hook(fn)`.

### `gateway/__main__.py`

- `app["router"]` → `app["runtime"]`.
- `handle_lane_message` reads `trigger` from POST body.
- `handle_lane_open` creates DB node; calls `runtime.register_sse_handler()`.
- `handle_lane_command` context dict: `agent` key → `runtime` key.
- `router.abort_generation()` → `runtime.abort(node_id)`.
- Fanout table unchanged.

### `main.py`

```python
async def main():
    cfg     = load_config()
    runtime = Runtime(config=cfg)
    await runtime.start()   # loads modules, starts cron/heartbeat tasks
    tasks = []
    if cfg.gateway.enabled:
        tasks.append(create_task(gateway_mod.run(runtime, cfg.gateway)))
    for each enabled bridge:
        tasks.append(create_task(bridge_mod.run()))
    ...
```

### `bridges/discord/`, `bridges/matrix/`

- Remove `router: Router` param from `run()`.
- Add HTTP client; trigger detection moves into bridge code.
- `"trigger": bool` in POST body to `/v1/lane/message`.
- Group buffering if desired is bridge-local in-memory state.

### Modules

`register(agent: AgentLoop)` → `register(runtime: Runtime)`.

Modules with previously per-lane state:

| Module | Previous per-lane state | Migration |
|---|---|---|
| `memory` | nudge debounce per branch | `runtime.module_env["memory:last_nudge:{node_id}"]` |
| `equipment_manifest` | EM.md cache | `runtime.module_env["em_cache:{node_id}"]` |
| `heartbeat` | cursor node_id | field on Runtime directly |
| `cron` | cursor per job in CRON.json | unchanged |
| `web` | Playwright instance | field on Runtime |
| `knowledge` | librarian process handle | field on Runtime |
| all others | none | trivial rename |

Knowledge module `_post_turn_hook` pushes a background `InboundMessage` via
`runtime.push()`. Background cycles receive `CycleHooks(post_turn=[])`.

### Knowledge librarian: stays as subprocess

Batch processor against KùzuDB. Not a conversational agent. Only change:
`register(runtime)` instead of `register(agent)`.

---

## What Does NOT Change

- `db.py` schema logic (plus three new columns).
- `ai.py` — `LLM.stream()`.
- `attachments.py` — `save_upload()`, `build_content_blocks()`.
- The 6-stage cycle logic (moves to `cycle.py`).
- The SSE event types and wire format.
- The HTTP gateway API surface (`/v1/lane/*` routes).
- The workspace layout and cursor file conventions.
- `tools_search` BM25 deferred tool discovery.
- `context.py` hook pipeline stages and token budget logic.
- `AttachmentKind`, `Attachment`, `UserIdentity`, `ContentType`.
- All `AgentEvent` subtypes except `lane_node_id` removal.
- The knowledge librarian subprocess and IPC protocol.

---

## Migration Phases

### Phase 1 — Contracts
- Add `trigger: bool = True` to `InboundMessage`.
- Remove `GroupPolicy`, `ActivationMode`.
- Remove `lane_node_id` from `_AgentEventBase`.
- Add `Platform.SYSTEM`.

### Phase 2 — DB columns
- Add `author_name`, `attachment_paths`, `state_delta` columns.
- Add `get_ancestors_full()`.
- Update `add_node()` and `Node` dataclass.

### Phase 3 — Context: state delta replay + attachment reconstruction + multi-author formatting
- `_load_state_from_db()` replays deltas from full ancestor walk.
- `_load_from_db()` re-hydrates attachments from paths.
- `assemble()` prepends `[Name]: ` for multi-author branches.
- New tests covering: delta replay across trimmed context, checkpoint snapshots,
  state inherited across branch points.

### Phase 4 — `CycleHooks` + `AgentCycle`
- Write `cycle.py`.
- Port 6-stage loop.
- Stub `agent.py`.

### Phase 5 — `Runtime`
- Write `runtime.py` with semaphore-based task pool.
- `push()` computes and writes state delta.
- Port module `register()` signatures.
- Wire `main.py`.
- Stub `router.py`.

### Phase 6 — Gateway update
- Swap `app["router"]` for `app["runtime"]`.
- `handle_lane_message` passes `trigger` through.
- `handle_lane_open` → `runtime.register_sse_handler()`.
- `handle_lane_command` uses `runtime`.

### Phase 7 — Bridge unification
- Port Discord and Matrix to HTTP-only.
- Trigger detection into each bridge.
- Delete `GroupLane`, `GroupPolicy`, `ActivationMode`.

### Phase 8 — Cleanup
- Delete `agent.py`, `router.py` stubs.
- Update tests and `CLAUDE.md`.

---

## Open Questions

1. **Who computes the state delta?** `Runtime.push()` needs to know the previous
   state to diff against. Options: fetch the parent node's accumulated state by
   replaying ancestors at push time (a DB read per message); or trust the bridge
   to send only what changed (fragile). Push-time replay is correct and the read
   is cheap for short trees.

2. **Checkpoint frequency.** Every 20 nodes is a reasonable starting point but can
   be tuned. `Runtime.push()` checks if the new node's depth (ancestor count) is a
   multiple of N and if so writes the full merged state instead of just the delta.
   Depth can be queried cheaply with a COUNT on the ancestor CTE.

3. **`enabled_tools` delta format.** Storing the full list on each change (not a
   diff of the list) is simplest — lists are small and replace semantics are clear.
   `["web", "filesystem"]` replaces whatever was there before. A null/absent
   `enabled_tools` key in a delta means "no change", not "empty list".

4. **Semaphore try-acquire for hard rejection.** Track `_active` count manually
   alongside the semaphore, checked before `create_task`.

5. **`/v1/lane/open` name.** Stale but changing breaks existing clients. Keep it.

6. **Singleton module tasks (cron, heartbeat).** `Runtime.start()` creates these
   as plain `asyncio.Task`s. They push `InboundMessage(trigger=True)` with their
   stored cursor node_id. Whether they contend for the semaphore or get a reserved
   path is TBD — simplest is normal contention to start.
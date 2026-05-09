# TinyCTX Refactor Plan

## Goal

Make everything stateless. Each `AgentCycle` is self-contained: it opens
the DB, reads state, wires its own context, runs, writes results, and
disappears. `Runtime` is a thin task spawner. `Context` is a pure
assembly pipeline.

---

## Problems with current code

### `runtime.py` is thick
- Builds `Context` objects itself via `_make_context()`
- Reads session state from DB via `_compute_state_delta()` and
  `_maybe_write_checkpoint()`
- Holds `_prompt_registrations` and `_hook_registrations` to replay onto
  every Context it manufactures
- `_ContextProxy` intercepts module calls at startup just to collect them
  for later replay
- `_ToolRegistry` wraps `ToolCallHandler` construction unnecessarily

### `context.py` is bloated
- `set_db()` / `set_tail()` are post-construction setters that allow
  half-initialized objects — constructor should take everything upfront
- `self.state` is a grab-bag that mixes persistent session state with
  ephemeral per-turn values; neither belongs here
- `_load_state_from_db()` is exposed as a public method so Runtime can
  call it — Context's internals leaking out
- `set_cursor_callback()` is AgentCycle's cursor concern, not Context's
- `assemble()` side-channels return values through `self.state` instead
  of returning them

### `modules/memory/__main__.py` registers on Runtime
- Uses a shim to call `runtime.context.register_prompt()` and
  `runtime.context.register_hook()` — these go through `_ContextProxy`
  which just collects them for replay
- Consolidation hook reaches into `agent.context.tail_node_id` and
  `agent._db` — live cycle state from module startup code

### `register()` serves two surfaces
- Singleton setup (store, indexer, embedder) should be `register_runtime`
- Per-cycle wiring (prompt providers, pre-assemble hook) should be
  `register_agent`

---

## New architecture

### `Runtime` — thin spawner

```
InboundMessage
  → write user node to DB (with state_delta if identity fields changed)
  → if trigger: create_task(AgentCycle(node_id, permission_level, config, module_registry).run())
  → return new tail node_id
```

Runtime holds:
- `config: Config`
- `db: ConversationDB`  (shared, for writing inbound nodes only)
- `module_registry: ModuleRegistry`
- `commands: CommandRegistry`
- Semaphore + task set + abort events + SSE fanout

Runtime does NOT hold: models, tool_handler, context, prompt registrations,
hook registrations, _ContextProxy, module_env.

### `AgentCycle` — self-contained (lives in `agent.py`)

Constructor: `AgentCycle(node_id, permission_level, config, module_registry)`

What it does itself:
1. Opens `ConversationDB` from `config.workspace.path`
2. Walks DB from `node_id` to load session state
3. From session state picks model name (falls back to `config.llm.primary`)
4. Builds `LLM` instance(s) from config
5. Builds `ToolCallHandler`, registers `tools_search` as always-on
6. Constructs `Context(db, tail_node_id, token_limit, image_tokens)`
7. Calls `module_registry.register_agent(self)` — modules wire hooks/prompts
8. Runs generation loop, yields `AgentEvent`

AgentCycle holds:
- `self.db: ConversationDB`
- `self.context: Context`
- `self.tool_handler: ToolCallHandler`
- `self.state: dict`  (the reconstructed session state from DB walk)
- `self.models: dict[str, LLM]`
- `self.config`, `self.permission_level`, `self.module_registry`

`self.state` is the persistent branch state read from DB. Modules that need
to share ephemeral data within a turn close over local variables inside
`register_agent()` — no shared scratchpad dict needed.

### `Context` — pure assembly pipeline

Constructor: `Context(db, tail_node_id, token_limit, image_tokens_per_block)`

- No `self.state`
- No `set_db()` — everything required at construction time
- `set_tail(node_id)` exists only to advance the cursor as the
  cycle writes tool-call and tool-result nodes mid-turn
- `assemble()` returns `(messages: list[dict], meta: AssembleMeta)` where
  `AssembleMeta` is a small dataclass with `tokens_used`, `tokens_pre_trim`,
  `was_trimmed`
- `run_async_hooks(stage)` stays async, same signature
- `_load_state_from_db` is removed from Context entirely — session state
  is loaded by `db.load_session_state()` in AgentCycle at construction time

### `ModuleRegistry` — its own file `module_registry.py`

```python
class ModuleRegistry:
    def register_runtime(self, runtime): ...   # called once at startup
    def register_agent(self, cycle): ...        # called per AgentCycle
```

Runtime calls `_load_modules()` which imports each module and calls
`mod.register_runtime(runtime)` if the function exists.

Every `AgentCycle.__init__` calls `module_registry.register_agent(self)`.

### Module contract — two optional functions

```python
def register_runtime(runtime: Runtime) -> None:
    # Singletons: open store/indexer/embedder as locals here.
    # Register tools on runtime's ToolCallHandler template.
    # Register commands.
    # Register background hooks (post-turn).
    # Define register_agent() as a closure over the singletons.

def register_agent(cycle: AgentCycle) -> None:
    # Register prompt providers on cycle.context.
    # Register pre-assemble hooks on cycle.context.
    # Typically defined as a closure inside register_runtime so it can
    # capture singletons (store, indexer, embedder) without module_env.
```

Singletons created in `register_runtime` are captured by `register_agent`
via closure. Each `AgentCycle` gets its own fresh per-cycle resources
(e.g. a new playwright instance) created inside `register_agent` — nothing
is shared between concurrent cycles. `module_env` is gone entirely.

---

## `ctx.state` elimination

Currently `ctx.state` is used for two purposes:

**1. Ephemeral hook-to-hook communication (e.g. memory)**

`_pre_assemble_async` writes `ctx.state["memory_search_results"]`.
The prompt provider reads it.

Fix: both are closures registered in the same `register_agent()` call.
They share a local list via closure:

```python
def register_agent(cycle):
    results = []

    async def _pre_assemble(ctx):
        results[:] = await search(...)

    cycle.context.register_hook(HOOK_PRE_ASSEMBLE_ASYNC, _pre_assemble)
    cycle.context.register_prompt("memory_search", lambda ctx: format(results))
```

**2. `assemble()` return values (`tokens_used`, `budget_trimmed`)**

Fix: `assemble()` returns `(messages, AssembleMeta)` directly.

---

## Session state & checkpointing

Both helpers live in `db.py` — it already owns all node I/O:

```python
def load_session_state(db: ConversationDB, node_id: str) -> tuple[dict, int]:
    # Walks ancestor chain tip→root, merges state_delta JSON objects.
    # Stops early at a node with "_checkpoint": true.
    # Calls write_checkpoint_if_needed.
    # Returns (state, depth) where depth is the number of nodes visited.

def write_checkpoint_if_needed(
    db: ConversationDB, node_id: str, state: dict, depth: int, threshold: int
) -> None:
    # If depth > threshold, writes a full checkpoint state_delta onto node_id.
```

`AgentCycle.__init__` calls `load_session_state`, then
`write_checkpoint_if_needed`. This logic moves out of Runtime entirely.

---

## `assemble()` return value

```python
@dataclass
class AssembleMeta:
    tokens_pre_trim: int
    tokens_used: int
    was_trimmed: bool

def assemble(self, tools: list[dict] | None = None) -> tuple[list[dict], AssembleMeta]:
    ...
```

AgentCycle reads `meta.tokens_used` directly for the 80%/95% log.

---

## Background branches

`push_background` is removed. Background branches are spawned by calling
`runtime.push()` with a synthetic `InboundMessage`:

```python
msg = InboundMessage(
    tail_node_id=branch_node_id,
    text=nudge_message,
    trigger=True,
    author=UserIdentity(platform=Platform.INTERNAL, user_id="system", username="system"),
    permission_level=100,
)
await runtime.push(msg)
```

Background cycles are indistinguishable from normal cycles. There is no
`is_synthetic` flag. Modules that spawn background branches (e.g. memory
consolidation) are responsible for not recursing infinitely — they do this
naturally by checking a condition (e.g. token delta) before spawning.

---

## File-by-file changes

| File | Change |
|---|---|
| `runtime.py` | Remove `_ContextProxy`, `_ToolRegistry`, `_make_context`, `_compute_state_delta`, `_maybe_write_checkpoint`, `_prompt_registrations`, `_hook_registrations`, `register_pre_assemble_hook`, `push_background`, `_run_background_cycle`. `_load_modules` calls `register_runtime`. `_process` constructs `AgentCycle(node_id, permission_level, config, module_registry)`. |
| `agent.py` | New `AgentCycle` replaces the stub. Constructor is self-contained (opens DB, loads state, builds LLM/tools/context, calls `module_registry.register_agent`). `run()` is the existing generation loop from `cycle.py`, adapted. |
| `cycle.py` | Deleted. Its generation loop moves into `agent.py:AgentCycle.run()`. |
| `context.py` | Constructor takes `(db, tail_node_id, token_limit, image_tokens_per_block)`. Remove `self.state`, `set_db`, `set_tail`, `set_cursor_callback`. Add `set_tail_node_id(node_id)` for mid-turn cursor advance. `assemble()` returns `(messages, AssembleMeta)`. `_load_state_from_db` stays private. |
| `module_registry.py` | New file. `ModuleRegistry` class. Scans `modules/`, imports, calls `register_runtime` and (per-cycle) `register_agent`. |
| `modules/*/` | Replace `register(agent)` with `register_runtime(runtime)` + `register_agent(cycle)`. Memory module: singletons in `register_runtime`, hook+prompt in `register_agent` via closure over singletons. |

---

## What does NOT change

- `db.py` — untouched
- `contracts.py` — untouched  
- `utils/tool_handler.py` — untouched
- `utils/bm25.py` — untouched
- `utils/attachments.py` — untouched
- `utils/commands.py` — untouched
- `ai.py` — untouched
- `config.py` — untouched
- Gateway / bridges — see `runtime.py` public API below

### Runtime public API (unchanged from bridge perspective)

```python
Runtime(config)
await runtime.start()
await runtime.push(msg) -> str | None
runtime.abort(node_id) -> bool
runtime.register_sse_handler(node_id, queue)
runtime.unregister_sse_handler(node_id, queue)
await runtime.shutdown()
```
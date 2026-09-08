# CODEBASE.md — TinyCTX

> Auto-generated. Update this file when you make changes to the code.
> This file is WHERE things are, not HOW they work. One line per module: what it does + which files. Don't document what's obvious from reading the code.

## What TinyCTX Is

A context-efficient agentic assistant framework. Configure a language model, pick a bridge (CLI, Discord, or HTTP gateway), and get a persistent, tool-using AI agent with memory consolidation, scheduled heartbeats, concurrent forks, and web browsing.

---

## Project Layout

```
TinyCTX/
├── __main__.py         CLI entrypoint (tinyctx onboard|start|stop|status|launch)
├── main.py             Async application entrypoint; starts gateway + bridges
├── contracts.py        Pure data contracts (dataclasses, enums). No I/O. All other layers import from here.
├── runtime.py          Runtime — owns DB, UserStore, ModuleRegistry, CommandRegistry; routes events
├── agent.py            AgentCycle — one execution turn; streaming inference + tool loop
├── ai.py               LLM / Embedder async clients (OpenAI-compat SSE streaming)
├── context.py          Context — assembles message list for the LLM; hook pipeline; token budgeting
├── db.py               ConversationDB — SQLite-backed conversation tree
├── module_registry.py  Loads modules from modules/ and custom_modules/ and wires them into each AgentCycle
│
├── config/             Config loading (YAML → dataclasses)
├── users/              UserStore + User/PlatformIdentity models (SQLite)
├── commands/
│   ├── launch.py        tinyctx launch — attaches a bridge client
│   ├── start.py         tinyctx start  — docker compose up for the resolved instance
│   ├── stop.py          tinyctx stop   — docker compose down for the resolved instance
│   ├── status.py        tinyctx status
│   └── onboard.py       tinyctx onboard — delegates to onboard/
├── utils/
│   ├── instance.py      Shared instance-directory resolution (--dir / CWD .tinyctx / ~/.tinyctx)
│   ├── tool_handler.py  ToolCallHandler — register/enable/execute tools
│   ├── commands.py      CommandRegistry — slash-command dispatch for bridges
│   ├── sanitize.py       sanitize_brackets() / sanitize_special_tokens()
│   ├── attachments.py   Attachment processing (images, PDFs, text, binary)
│   └── bm25.py          BM25 keyword search (used for tool_search and memory)
│
├── bridges/
│   ├── cli/__main__.py      Interactive terminal UI (rich TUI, session restore)
│   └── discord/             Discord bridge (discord.py) — see below
│
├── gateway/__main__.py      HTTP/SSE gateway (aiohttp, /v1/chat endpoint)
│
├── onboard/            Interactive first-run setup wizard
│   ├── __main__.py     Orchestrates setup steps
│   ├── providers_setup.py
│   ├── gateway_setup.py
│   ├── bridges_setup.py
│   └── workspace_setup.py
│
├── custom_modules/     User-defined plugins, gitignored (same interface as modules/)
└── modules/            Auto-discovered plugins (see Module System below)
    ├── comfyui/        generate_image_comfyui tool
    ├── cron/           Cron scheduler
    ├── concurrency/    Concurrent Forks — spawn_fork / nudge_fork
    ├── ctx_tools/      Context-assembly hooks: dedup, cot_strip, tool-output trim/truncate, tokenade
    ├── equipment_manifest/  Agent's self-description of available tools
    ├── filesystem/     view / write_file / edit_file / grep / glob_search tools
    ├── heartbeat/      Periodic agent turns on a background branch
    ├── mcp/            MCP server integration
    ├── memory/         Knowledge graph (LadybugDB property graph + librarian agents)
    ├── present/        present() tool — delivers files to users via bridges
    ├── rag/            Semantic search over workspace/memory/ (BM25 or embeddings)
    ├── shell/          shell tool
    ├── skills/         use_skill tool
    ├── sysops/         User/permission management + /model command + set_active_model tool
    ├── system_prompt/  Injects SOUL.md, AGENTS.md into system prompt
    ├── todo/           todo_read / todo_write tools (per-session task list)
    └── web/            web_search / open_url tools (DuckDuckGo + Camoufox)
```

---

## Core Data Flow

```
Inbound message (bridge)
  → UserStore.resolve_user()       — get/create User
  → Runtime.push(InboundMessage, reply_queue)  — write user node to DB, spawn task
    → AgentCycle.run(node_id)
        1. Load session state from DB
        2. Build LLM(s), ToolCallHandler, Context
        3. ModuleRegistry.register_agent(cycle) — wire modules in
        4. Loop (up to max_tool_cycles):
             a. context.assemble() → message list
             b. LLM.stream()       → TextDelta / ToolCallAssembled / LLMError
             c. If tool calls: execute, add results to context, loop
             d. If no tool calls: emit AgentTextFinal, run post-turn hooks
        5. Yield AgentEvent stream → put into reply_queue
  → Bridge drains reply_queue and renders events (streaming text, tool status, files)
```

All bridges use an `asyncio.Queue` (`reply_queue`) passed to `Runtime.push()`. `Runtime._process()` puts each event into the queue; a `None` sentinel signals turn completion.

---

## Key Contracts (`contracts.py`)

Frozen dataclasses/enums used for all cross-layer communication.

| Type | Purpose |
|------|---------|
| `Platform` | Enum: CLI, DISCORD, MATRIX, CRON, API, SYSTEM |
| `SessionEnvironment` | `platform`, `agent_name`, `server_name`, `channel_name` — carried by every `InboundMessage` |
| `InboundMessage` | Canonical message envelope from bridges: `tail_node_id`, `author`, `env`, `text`, `attachments`, `trigger` |
| `AgentTextChunk` | One streaming token |
| `AgentTextFinal` | End of turn (or non-streaming full text); `.suppressed` set when reply was `NO_REPLY` sentinel |
| `AgentToolCall` / `AgentToolResult` | Tool invocation / result event |
| `AgentError` | LLM error or cycle limit reached |
| `AgentOutboundFiles` | File paths to deliver to the user (from `present()` tool) |
| `ToolCall` / `ToolResult` | Internal tool call/result (distinct from Agent* event types) |
| `Attachment` | File attached to an inbound message |
| `IMAGE_BLOCK_PREFIX` | Sentinel prefix returned by filesystem `view()` for images |
| `MANUAL_LAUNCH_ATTR` | Module-level flag; bridges with this skip auto-start |

---

## Database (`db.py`)

SQLite WAL-mode database at `<instance>/data/agent.db` (not workspace/ — the agent's own filesystem tools never see it). Conversation state is a **tree of nodes**, each with a `parent_id`.

Columns: `id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id, attachment_paths, state_delta, flags`

Session state is reconstructed by walking the ancestor chain and merging `state_delta` JSON (most-recent wins); `"_checkpoint": true` nodes stop the walk early. `flags` is a JSON array column used by modules to mark nodes without a dedicated column.

Key methods (`db.py`):
- `add_node(parent_id, role, content, ...)` → `Node`
- `get_ancestors(node_id)` → `[Node]` root→tip order
- `load_session_state(node_id)` → `(dict, depth)`
- `get_state(node_id, key, default=None)` / `set_state(node_id, key, value)` — single-key read/merge-write; prefer these over `load_session_state`/`update_node_state_delta`
- `flag_branch(node_id, flag)` / `get_nodes_without_flag(flag)`

The `"model"` session-state key (read in `agent.py`) holds a branch-scoped LLM override; written by `modules/sysops/` (`/model` command and `set_active_model` tool).

---

## Context Assembly (`context.py`)

`Context.assemble()` builds a `list[dict]` (OpenAI message format) from:
1. Registered **prompt providers** (`register_prompt`) — concatenated into the system message
2. **DB history** — `_load_from_db()` walks ancestor chain
3. A **hook pipeline**: `HOOK_PRE_ASSEMBLE_ASYNC`, `HOOK_PRE_ASSEMBLE`, `HOOK_FILTER_TURN`, `HOOK_TRANSFORM_TURN`, `HOOK_POST_ASSEMBLE`

User turns are prefixed `【author_id】: ` after the hook pipeline. Special-token sanitization (`utils/sanitize.py`) runs last, after merge of adjacent same-role messages, over every entry regardless of role/origin. Token budget enforcement trims oldest non-system turns to fit.

Returns `(messages, AssembleMeta)` with `tokens_pre_trim`, `tokens_used`, `was_trimmed`.

Deferred (`role="user"`) prompt providers (equipment_manifest footer, concurrency roster) are spliced in right before the trailing run of consecutive user turns.

Thinking (`<think>...</think>`) is stored inline on the assistant `content` (no separate column); `_render()` peels one leading block into `reasoning_content` for replay. `modules/ctx_tools`'s `trim_thinking` controls how much survives into later turns.

### `utils/sanitize.py`
- `sanitize_brackets()` — Unicode bracket homoglyph → ASCII (protects `【author】:` delimiter)
- `sanitize_special_tokens()` — strips LLM special/control tokens (`<|im_start|>`, `[INST]`, Harmony `<|channel|>`, etc.), runs to a fixed point (capped at `_MAX_SANITIZE_PASSES = 20`)

---

## LLM Client (`ai.py`)

`LLM` — async OpenAI-compatible streaming client (Anthropic compat, OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp).

- `LLM.stream(messages, tools, priority=10)` yields `TextDelta | ThinkingDelta | ToolCallAssembled | LLMError`
- Retries on `ClientConnectionError` (tenacity, 3 attempts)
- `budget_tokens` → Anthropic extended thinking; `cache_prompts` → `cache_control: ephemeral` on last system message
- Per-model context budget: `ModelConfig.context` (config/`__main__.py`, default `16384`), wired into `Context(token_limit=...)` by `agent.py`
- `Config.token_fuzz` (default `1.1`) — global multiplier on counted tokens in `Context._count_tokens`

`Embedder` — async embedding client. `embed(texts, priority=10, kind="default")` is the sole entry point; chunks into `batch_size` requests, falls back to per-item retry on batch failure (concurrent via `asyncio.gather`), failed items come back `None`. `kind`: `"query"` → `query_template`, `"document"` → `document_template`, else raw text.

### Priority queue
Module-level priority queue in `ai.py` admission-controls every `LLM.stream()`/`Embedder.embed()` call. Lower `priority` runs first (FIFO ties).
- `configure_parallel(n)` — max concurrent in-flight requests (`config.parallel`, default 3), called once in `main.py`
- Convention: `0` user-facing cycle, `5` query-time embeddings, `15` librarian/dedup background loops, `20` RAG indexer batch embedding

---

## Tool System (`utils/tool_handler.py`)

`ToolCallHandler`:
- `register_tool(fn, always_on=False, min_permission=25)` — builds JSON schema from signature+docstring
- `enable(name)` — turns a tool on for the current cycle
- `tools_search(query)` — BM25 search over tool names+descriptions
- `get_tool_definitions(caller_level, minimal_tokens)` — OpenAI-format tool defs for enabled+permitted tools
- `execute_tool_call(tool_call, caller_level)` — dispatches sync (thread-pool) or async
- `apply_overrides(overrides)` — applies config `tool_overrides:` after modules register

Permission levels 0–100. `_python_type_to_json_schema` (schema generation from type annotations) lives here too.

---

## Module System (`module_registry.py`)

Modules live under `TinyCTX/modules/<name>/`. Auto-discovered if they have `__main__.py` or `__init__.py`. Every module is a `Module` subclass now (`TinyCTX/module.py` + `TinyCTX/decorators.py`, MODULES-PLAN-P1.md — **P3 complete**): one class with `@tool`/`@hook`/`@command`/`@prompt`-tagged methods, instantiated once (process lifetime) and walked by the loader — no registration calls in module code. `settings` is a declarative schema merged via `resolve_settings()` onto `self.config`, replacing the old per-module `EXTENSION_META` + eight-line config-merge boilerplate. The legacy function-pair shape (`register_runtime(runtime)`/`register_agent(cycle)`) is gone — a module lacking a `Module` subclass is skipped with a warning, not treated as function-based. `for-contributors/module_template/__init__.py` is the one annotated reference file for writing a new module.

- `@hook` types actually wired: `PRE_ASSEMBLE`/`PRE_ASSEMBLE_ASYNC`/`FILTER_TURN`/`TRANSFORM_TURN`/`POST_ASSEMBLE`/`POST_COMPLETION` → `cycle.context.register_hook`; `POST_TURN` → `cycle.post_turn_hooks.append`; `STARTUP` → called once with `runtime` at module-class load time (`_register_module_class`); `TURN_START` → called once with `cycle` at per-cycle wiring time (`_wire_module_instance`) — no dedicated emitter exists yet, this is the closest per-cycle hook point, and it's also where a `@tool`/hook needing *live* cycle state (`cycle.caller`, `cycle.outbound_events`, `cycle.tool_handler.enable()`, ...) gets registered imperatively, since a `@tool` method itself receives only its own declared arguments — see `modules/present`'s docstring. `STREAM_TEXT`/`STREAM_START`/`STREAM_END` (need stream-pass `Scratch`, not built) and `SHUTDOWN`/`BACKGROUND`/`DELIVER`/`INBOUND` (runtime-scoped or no consumer yet) log a warning and register nowhere if `@hook`'d.
- `Context.assemble()` creates one `hooks.Scratch()` per call, passed to any `PRE_ASSEMBLE`/`FILTER_TURN`/`TRANSFORM_TURN`/`POST_ASSEMBLE`/prompt-provider handler that declares a trailing `scratch` parameter (`hooks.handler_wants_scratch`); dropped when `assemble()` returns — this is how a module shares data across its own hook stages within one assemble pass without closures (see `ctx_tools`' dedup/trim). `ctx.state` is the coarser, longer-lived sibling — persists across every `assemble()` call within one `AgentCycle` (used by `output_parser`'s nudge budget, `memory`'s `memory_block` cache).
- `Context.db` is a public property (`ConversationDB`) so a `ctx`-only hook/prompt body can walk ancestors without needing `agent`/`cycle` (e.g. `equipment_manifest`, `skills`).
- `ToolError` (`TinyCTX.module.ToolError`) is caught by `ToolCallHandler.execute_tool_call` and rendered as a normal successful call (`success: True`, the message as the result text) — what a `@tool` raises for an expected failure instead of hand-writing `return "Error: ..."`. Not yet retrofitted onto every existing tool's ad-hoc error string (mechanical, deferred).
- `CommandRegistry.dispatch()` delivers a handler's non-None return value itself via `context["send"]`/`["console"]` (`_deliver`) — a `@command`/inline handler returns its output instead of calling `send()` directly. Converted: `sysops`'s `/model`, `memory`'s `/memory librarian`+`/memory stats`, and `runtime.py`'s inline `/user info`/`/user rename`/`/user modify_permissions` (not `@command`-able since `Runtime` isn't a `Module`, but same return-value convention).
- `memory`'s `memory_block` prompt race (a detached background task with no ordering guarantee against `assemble()`) is fixed, not just ported — see the `memory` entry below.

---

## User System (`users/`)

`User` — `username`, `permission_level` (0–100), list of `PlatformIdentity`, freeform `meta` dict.

`UserStore` — SQLite-backed, in `users/`. `resolve_user(platform, user_id, username, display_name)` is the hot path. In-memory LRU cache on `(platform, user_id)` and `username`.

Slash commands (registered by `Runtime`): `/user grant <username> <level>` (requires level 100), `/user info <username>`, `/user rename <username> <new>`.

---

## Runtime (`runtime.py`)

`Runtime` owns: `db` (ConversationDB), `users` (UserStore), `commands` (CommandRegistry), `module_registry`, `_semaphore` (max concurrent cycles, default 8).

`push(InboundMessage, reply_queue)` → builds content blocks, computes `state_delta`, writes user node, spawns `_process()` if triggered. `_process()` runs an `AgentCycle` and streams events into `reply_queue`. `abort(node_id)` cancels a running cycle.

---

## Bridges

### CLI (`bridges/cli/__main__.py`)
- `MANUAL_LAUNCH = True` — starts via `tinyctx launch cli`
- Rich TUI, session-restore cursor at `<instance>/data/cursors/cli`
- Provider presets for OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp, custom
- `agent_name` option under `bridges.cli.options`
- `default_to_resume: true` reattaches to the previous session instead of branching fresh
- Streaming render helpers: `_split_blocks` / `_emit_text` / `_print_block` — append-only, block-boundary flushing, no cursor-repositioning escapes

### Discord (`bridges/discord/`)

```
bridges/discord/
  __main__.py   Entry point — instantiates DiscordBridge and calls run()
  bridge.py     DiscordBridge — client setup, event routing (on_message/on_ready),
                access-control checks, attachment fetching, cursor wrappers, thread handling
  turn.py       handle_turn() + typing_keepalive() — drains reply_queue, typing indicator, reply chunking
  commands.py   sync_app_commands(), handle_reset_interaction(), handle_shutdown_interaction(),
                handle_command_interaction()
  cursors.py    CursorStore — persists discord.json + discord_msg_nodes.json under data/cursors/;
                make_session_node() helper
  compat.py     CompatRules — hot-reloads compat.json, proxy-bot delay rules (e.g. Tupperbot)
  mentions.py   humanize_mentions() / dehumanize_mentions()
  compat.json   Per-pattern delay rules (data, not Python)
```

Key config (`bridges.discord.options`): `token_env`, `allowed_users_dm`, `allowed_servers`, `admin_users`, `prefix_required`, `command_prefix`, `reset_command`/`shutdown_command`, `max_reply_length`, `typing_indicator`/`typing_on_thinking`/`typing_on_tools`/`typing_on_reply`.

`agent_name` comes from `_bot_display_name(guild)` (nickname, falls back to global display name). Thread branching forks a new DB branch per thread. Cursors (`dm:<uid>`, `group:<cid>`, `thread:<tid>`) persist in `discord.json`. Each trigger message gets its own `push()`; concurrent turns fork off `settled_tail` (see `concurrency` module).

### Gateway (`gateway/__main__.py`)
aiohttp HTTP server: `/v1/chat` (OpenAI-compat SSE), `/v1/health`, `api_key` auth.

---

## Notable Modules

### `system_prompt` (migrated to `Module`/decorators — MODULES-PLAN-P1.md P3)
`SystemPrompt(Module)` — injects SOUL.md, AGENTS.md, TOOLS.md via three `@prompt` methods, backed by providers built once in `@hook(HookType.STARTUP)`. Overrides `resolve_settings()` to deep-merge each `{file, priority}` setting instead of the base's whole-value replace (a partial override, e.g. just `soul.priority`, must not drop `soul.file`).

### `output_parser` (migrated — MODULES-PLAN-P1.md P3)
`OutputParser(Module)` — `@hook(HookType.POST_COMPLETION)`. Its nudge-budget state (`liquid_notified`/`nudge_count`) lives on `ctx.state["output_parser"]` (Context's own per-cycle state bag) rather than instance state, since the hook itself is process-lifetime/shared but the budget is genuinely per-AgentCycle.

### `present` (migrated — MODULES-PLAN-P1.md P3)
`present(paths)` tool. Emits `AgentOutboundFiles` events. Registered imperatively from `@hook(HookType.TURN_START)`, not `@tool` — it needs live `cycle.outbound_events`/`cycle.context.tail_node_id`/`cycle.trace_id` at call time, which a `@tool` method has no way to receive (its declared parameters are the model-visible schema). This "wire a closure from TURN_START" pattern repeats for any tool needing live per-cycle state until Part 2's facade exists.

### `sysops` (migrated — MODULES-PLAN-P1.md P3)
`Sysops(Module)` — `user_list`/`user_info`/`user_modify_permissions`/`user_rename`/`user_merge`/`set_active_model` tools wired from `@hook(HookType.TURN_START)` (same live-cycle-state reason as `present`). `/model` is a genuine `@command` — its handler only needs `runtime` (cached at `@hook(HookType.STARTUP)`) plus the per-call `context` dict `CommandRegistry.dispatch()` already passes every handler.

### `rag` (migrated to `Module`/decorators — MODULES-PLAN-P1.md P3)
Indexes named databank folders under `workspace/rag/` — BM25 or embedding search via `rag_search`/`set_auto_rag_databanks` tools.
- `lorefile.py` — parses `*.md` YAML frontmatter (`name`, `mode`, `keys`, `secondary_keys`, `constant`, `selective`, `selective_logic`, `case_sensitive`, `whole_words`, `disabled`) for keyword-triggered lore entries; `convert_lorebook_json` migrates legacy SillyTavern JSON lorebooks
- `databanks.py` — `FilesDataBank` (only databank kind), `_entry_cache` keyed by `(path, mtime)`
- `Rag(Module)` in `__init__.py` — former `_RagState` dataclass fields are now instance attributes, built once in `@hook(HookType.STARTUP)`. The auto-inject prefetch stays `@hook(HookType.PRE_ASSEMBLE_ASYNC)` (it does a real `embed()` network call, so it can't be the synchronous `PRE_ASSEMBLE` `Context.assemble()` calls inline) and caches results on `ctx.state["rag_auto_results"]` for a paired `@prompt` to read (`Scratch` doesn't exist yet at that point — it's created inside `assemble()`, which hasn't started). `rag_search`/`rag_list_databanks` are plain `@tool`s (process-lifetime state only); `set_auto_rag_databanks` needs live `cycle.context`/`cycle.db`, so it's wired imperatively from `@hook(HookType.TURN_START)` like `modules/present`.
- Config: `default_auto_targets` in `EXTENSION_META["default_config"]`

### `memory` (v2, migrated — MODULES-PLAN-P1.md P3, incl. the plan's flagged race fix)
Scoped LadybugDB property-graph knowledge store at `<instance>/data/memory/memory.lbug`. Design doc: `modules/memory/PLAN.md`.
- `Memory(Module)` in `__init__.py` — `LibrarianRunner`, `GraphDatabase`/`GraphDB` singletons built once in `@hook(HookType.STARTUP)`. `/memory librarian`/`/memory stats` are genuine `@command`s (only need `self.*` + the per-call `context` dict); `call_librarian` is a plain `@tool`; `search_memory`/`memory_stats` (scope-bound) and the pressure-ingest post_turn hook need live `cycle.context`/`.db`, wired from `@hook(HookType.TURN_START)`.
- **The `memory_block` race is fixed, not just ported.** The old design fired a detached `asyncio.create_task` at cycle start with no ordering guarantee against `assemble()` — a later assemble() pass in a multi-step tool-calling loop could read a stale block, or `None` from initialization, indistinguishable from a real "nothing relevant" result. Now `@hook(HookType.PRE_ASSEMBLE_ASYNC)` (`refresh_memory_block`) directly `await`s the block computation, bounded by `passive_rag.block_timeout_seconds` (new setting, default 3.0s), caching the result on `ctx.state["memory_block"]` for a paired `@prompt` to read; a timeout logs a warning and keeps whatever `ctx.state` already had (persists across assemble() passes within one cycle, unlike `Scratch`) instead of silently resolving to `None`. Recomputing every pass (not once per cycle) also means a later pass reflects tool calls that already ran. Tests: `tests/test_memory.py::TestMemoryBlockJoinPoint`.
- `graph.py` — `Entity`/`Relation` schema, `VectorIndex` (in-memory, dirty-set invalidated)
- `scopes.py` — `resolve_scopes(env, active_users)`; scope grammar `global` | `kind:target`
- `tools.py` — all tools in one file: `search_memory`, `memory_stats`, `call_librarian` (main agent); `memory_add_entity`, `memory_update_entity_description`, `memory_set_entity_pinned`, `memory_set_entity_scope`, `memory_delete_entity`, `memory_set_relationship`, `memory_delete_relationship`, `memory_merge_into` (librarian-only)
- `extractor.py` — ingests unvisited conversation branches into the graph
- `reviewer.py` — loads flaggers from `flaggers/` (orphaned, description_length, too_many_edges, over_pinned, decay_candidate, fuzzy_names, edge_bloat), persisted issue queue at `data/reviewer_queue.json`
- `deduper.py` — embedding pass + semantic dedup, cache at `data/dedup_cache.db`
- `librarian_common.py` — shared agent-loop/tool-handler/`nodes_to_text` plumbing
- `format.py` — `format_entity()`/`format_entities()` at three detail levels (`low`/`medium`/`high`); config `memory.formatting: {injection_detail, desc_truncate_chars}`
- `migrate.py` — one-shot v1→v2 migration (`graph.lbug` → `memory.lbug`)
- `decay.py` / `dedup_agents.py` / `librarian_agents.py` — inert deprecation stubs
- Tests: `tests/test_memory.py`

### `heartbeat`
Fires periodic agent turns on a background DB branch. Slash command: `/heartbeat run`.

### `cron` (v2, SQLite-backed; migrated — MODULES-PLAN-P1.md P3)
`Cron(Module)` — `add_cron`/`list_cron`/`remove_cron` tools; jobs are rows in a SQLite store under `config.data.path/cron.db` (never `workspace/`, so the agent's own filesystem tools can't create/edit jobs — closes v1's indirect-prompt-injection path). `CronStore`/`_CronRunner` (the background scheduler) are built once in `@hook(HookType.STARTUP)`; the three tools need live `cycle.caller`/`.context`/`.db`/`.config.permissions`, so they're wired imperatively from `@hook(HookType.TURN_START)` like `modules/present`.

### `filesystem` (migrated — MODULES-PLAN-P1.md P3)
`Filesystem(Module)` — `view`, `write_file`, `edit_file`, `grep`, `glob_search` tools. Write tools sandboxed to `workspace/`; read tools can also reach `filesystem.read_only_paths` from config.yaml. `view()` returns images via `IMAGE_BLOCK_PREFIX`, unwrapped by `agent._execute_tool`. All five wired imperatively from `@hook(HookType.TURN_START)` (same reason as `present`) since `file_read_state` (the read-before-write staleness tracker) must be fresh per `AgentCycle` — which, since `runtime.py` constructs a new `AgentCycle` per turn rather than reusing one per session, means it never actually survives across turns despite the docstring reading like session-lifetime state; preserved exactly as-is, not "fixed."

### `shell` (migrated to `Module`/decorators — MODULES-PLAN-P1.md P2)
`Shell(Module)` in `__init__.py` — `shell` tool, runs in workspace directory, Linux only. Proves `@tool` carries a callable permission classifier (`shell_perms.required_permissions_for_shell`, an imported function, not a method) and `listing_permissions` intact.
- `@hook(HookType.STARTUP)` resolves the shape policy and sandbox URL once (was per-cycle in `register_agent`); also rewrites `Shell.shell.__doc__` with the real configured `max_timeout`, since a `@tool` method's docstring — read by `register_tool()` for the model-visible schema — is otherwise fixed at import time
- Which commands a caller may run is decided entirely by granted capabilities (the single `permissions.template` in `config.yaml`, plus any per-user `permission_overrides`) via `perms.py`'s per-command classification — `min_permission`/tiered `policies`/`permissions.access_backend` are gone, permission_level was fully retired (see `TinyCTX/permissions.py`, `docs/PERMISSIONS-PLAN.md`)
- `validate.py` — AST-based command validation via `tree-sitter-bash`
- `policy.py` — compiles a shape-only policy (construct/redirect/glob shape, not capability rules) from `allow.yaml`'s `constructs` map
- `settings`: `default_timeout` (120), `max_timeout` (1200), `sandbox_url` (`"auto"` computes from `TINYCTX_INSTANCE`; empty string disables the sandbox and runs in the main container)
- Design doc: `modules/shell/PLAN.md`
- Tests: `tests/test_shell_policy.py` (shipped-YAML corpus), `tests/test_shell.py` (capability gating, fail-closed), `tests/test_shell_perms.py`/`tests/test_shell_perms_yaml.py` (per-command tag table)

### `web`
`web_search` (DuckDuckGo via `ddgs`) and `open_url` (Camoufox — anti-detect Firefox), plus `click`/`type_text`/`extract_text`/`extract_html`/`screenshot_browser`/`wait_for` acting on the last-loaded page.
- `config.web.headless`: `true` | `false` | `"virtual"` (default, Xvfb)
- Interstitial handling: `_settle_navigation()`, `_CHALLENGE_SELECTORS`, `_wait_for_dom_stable()`; budget `config.web.settle_timeout_ms`
- Screenshots → `workspace/outputs/browser/` (`config.web.output_dir`), inlined via `IMAGE_BLOCK_PREFIX` unless over `config.web.screenshot_max_bytes`

### `comfyui` (migrated — MODULES-PLAN-P1.md P3)
`ComfyUI(Module)` — a plain `@tool` (all setup is process-lifetime, done once in `@hook(HookType.STARTUP)`; unlike `present`/`sysops` it needs no live per-cycle state). If no workflows are configured the tool stays registered (decorators tag unconditionally) and returns a clear error on call, instead of the old "don't register the tool at all". Also fixed in passing: the old `EXTENSION_META` default for `api_key` was the literal string `"null"`, not `None` — truthy, so an unconfigured instance sent a literal `Authorization: Bearer null` header; the settings default is now real `None`.
`generate_image_comfyui(workflow, positive_prompt, negative_prompt, dimensions="1024x1024", seed=0)` tool.
- Workflow JSON files live in `<instance>/config/comfyui/<name>.json`, resolved via `utils/instance.py::runtime_config_dir()`
- `filter.py` — NudeNet-based safety filter (hard/soft blocked labels, censor-in-place)
- Marker substitution: `MARKER>>name<<MARKER` for `positive-prompt`, `negative-prompt`, `seed`, `width`, `height`
- `Permission.IMAGE_GEN` gating; config under `comfyui:` (host/port/api_key/timeout/unload_after/safety_filter)
- Outputs: `workspace/outputs/comfyui/`

### `ctx_tools` (migrated to `Module`/decorators — MODULES-PLAN-P1.md P2)
`CtxTools(Module)` in `__init__.py` — context-assembly hooks only, registers **no tools**. Settings schema replaces `EXTENSION_META`. Per-pass state (`suppressed_tool`, `last_user_idx`, `trimmed_calls`) lives on `scratch`, not closures.
- **dedup** — suppresses repeated identical tool call+result (`same_call_dedup_after`, default 2)
- **cot_strip** — strips `<think>` blocks per `trim_thinking` (`"all" | "auto" | "none"`, default `"auto"`)
- **trim** — replaces/truncates old tool-result turns (`tool_output.trim_after`/`truncate_after`/`max_chars`)
- **tokenade** — blocks turns over `tokenade_threshold` (default 20000) tokens
- **label_prefix_strip** (`_LabelPrefixStripHook`) — wired into `cycle.stream_text_hooks` from a `@hook(HookType.TURN_START)` method, since `STREAM_TEXT`'s object-protocol (reset/process/flush) has no decorator path yet
- Special-token sanitizing (`<|im_start|>`, `[INST]`) is a baseline pass in `context.py`'s own `assemble()`, not a ctx_tools hook

### `equipment_manifest` (migrated to `Module`/decorators — MODULES-PLAN-P1.md P2)
`EquipmentManifest(Module)` in `__init__.py` — renders `EM.md` (Jinja2) as a system prompt every turn.
- `@hook(HookType.STARTUP)` does the one-time EM.md/Jinja2-Environment setup (replaces `register_runtime`+`register_agent`'s per-cycle-repeated setup)
- `equipment_manifest` (role=system, static vars, `@prompt`) — cache-stable
- `equipment_manifest_footer` (role=user, volatile vars: `time`, `time_since_last_message`, `@prompt`) — from `EM_FOOTER.md` or a built-in default; `time_since_last_message` is precomputed by a paired `@hook(HookType.PRE_ASSEMBLE)` into `scratch.last_message_ts` (the plan's worked example for `@prompt` + scratch-feeding `@hook`)
- `trusted` resolved via `UserStore.get_user(author_id)` (username lookup, not `get_by_platform`)
- `em_path` config key resolution: `""` → `EM.md` next to module; `"workspace:X"` → under workspace root
- `@prompt`'s `priority` is fixed at class-definition time, so `prompt_priority` is no longer a live setting (was already unused in this repo's config)

### `concurrency` (migrated — MODULES-PLAN-P1.md P3)
`Concurrency(Module)` — `@hook(HookType.STARTUP)` caches `runtime`; `@hook(HookType.TURN_START)` wires the roster prompt + `spawn_fork`/`nudge_fork` tools imperatively (same live-cycle-state reason as `present`/`sysops`). Design doc: `docs/PLAN.md`. Registers `running_forks` roster prompt provider (role=user) plus:
- `spawn_fork(prompt)` → `run_id` — starts a run on a fresh branch off caller's head
- `nudge_fork(run_id, message)` — advisory one-way message to a peer

Lifecycle lives in `runtime.py`:
- `Run` — in-memory handle (`id`, `session_key`, `intent`, `root_node_id`, `status`, `inbox`)
- `_settled: session_key → node_id`
- `finish_run()`, one `asyncio.Lock` per session, `Exogenous(kind, role, content)` inbox entries
- Scoped by `Run.session_key` (bridge cursor key), not `SessionEnvironment`
- Capacity capped by `Runtime._semaphore` (`max_workers`, default 8)
- Tests: `tests/test_concurrency.py`

### `skills` (migrated — MODULES-PLAN-P1.md P3)
`Skills(Module)` — `use_skill(name)` tool. Loads `SKILL.md` from `workspace/skills/<name>/` (agentskills.io convention). Frontmatter `tools:` list enables deferred tools on load. Discovery/index/tag-tracking are plain `@hook`/`@prompt` (only ever touch `ctx`, via the now-public `ctx.db`); `use_skill`/`collapse_skill_categories` need live `cycle.tool_handler`/`cycle.context`, so they're wired from `@hook(HookType.TURN_START)` like `modules/present`.

(No `todo` module exists in this codebase — a stale entry describing one was removed here; MODULES-PLAN-P1.md's own `todo` example module is illustrative, not a real module in this repo.)

### `present`
`present(paths)` tool. Emits `AgentOutboundFiles` events.

### `mcp` (migrated — MODULES-PLAN-P1.md P3)
`MCP(Module)` — connects configured MCP servers once at `@hook(HookType.STARTUP)` (was per-cycle before migration, which reconnected — spawning subprocesses — on every single turn; that was waste the migration fixes, not a behavior this module relied on). `@hook(HookType.TURN_START)` cheaply re-registers already-discovered tools into each new cycle's `tool_handler`. The old `agent.reset()`-patching restart mechanism referenced a method that doesn't exist on `AgentCycle` (dead code, no test coverage) and was dropped rather than carried forward.

---

## Config (`config/`)

YAML, loaded from `<instance>/config.yaml` by default (or `--config`). Key top-level keys:

- `workspace.path` — default `<instance>/workspace`
- `data.path` — default `<instance>/data`
- `models` — dict of named model configs (`kind`, `base_url`, `api_key_env`, `model`, `max_tokens`, `temperature`, `supports_vision`, `tokens_per_image`)
- `llm.primary` / `llm.fallback`
- `context` — token budget
- `max_tool_cycles`
- `parallel` — max concurrent in-flight LLM/embedding requests (default 3)
- `bridges.<name>.enabled` / `bridges.<name>.options`
- `gateway.enabled` / `gateway.host` / `gateway.port` / `gateway.api_key`
- `logging.level`
- `permissions.minimal_tokens`
- `tool_overrides` — `<tool_name>: {always_on?, min_permission?}`, parsed into `Config.tool_overrides`

---

## Instance Layout (`utils/instance.py`)

Resolution order: `--dir` flag → nearest ancestor of CWD named `.tinyctx` → `.tinyctx/` child of CWD → `~/.tinyctx`.

```
<instance>/
├── config.yaml
├── config/                  Extra config (shell policy YAML). Read-only in container at /app/config
│                            (TINYCTX_CONFIG_DIR / TINYCTX_CONFIG_DIR_PATH). Resolve via
│                            utils/instance.py::runtime_config_dir().
├── .env                     KEY=VALUE per line. Loaded via load_instance_env() (override=True)
├── workspace/               Agent-authored content, visible to filesystem tools
│   ├── SOUL.md              Agent personality
│   ├── AGENTS.md            Sub-agent/persona definitions
│   ├── CRON.json            Scheduled jobs
│   ├── HEARTBEAT.md         Heartbeat instructions
│   ├── downloads/           Files/images sent by users via bridges
│   ├── uploads/             Large attachments saved instead of inlined
│   ├── skills/<name>/SKILL.md
│   └── rag/, memory/*.md    RAG corpus (distinct from data/memory/ graph)
└── data/                     TinyCTX-internal state, not visible to filesystem tools
    ├── agent.db              Conversation tree (SQLite)
    ├── users.db              UserStore
    ├── cursors/               Per-bridge session cursors
    └── memory/                LadybugDB graph (graph.lbug), librarian.log, dedup_cache.db
```

Docker Compose (`compose.yaml`, repo root) invoked with env vars (`TINYCTX_CONFIG_FILE`, `TINYCTX_WORKSPACE`, `TINYCTX_DATA`, `TINYCTX_PORT`, `TINYCTX_INSTANCE`, `TINYCTX_TAG`) computed by `utils/instance.py::compose_env()`.

Non-Docker launches set `TINYCTX_CONFIG_FILE` in the subprocess env; `main.py` reads it if present, else defaults to `config.yaml` relative to CWD.

---

## Dependency Notes

Key packages: `aiohttp`, `rich`, `questionary`, `mcp`, `tiktoken`, `structlog`, `tenacity`, `ddgs`, `playwright`, `pdfplumber`, `python-docx`, `croniter`, `python-dotenv`, `discord.py`, `jinja2`, `numpy`.

Python ≥ 3.14 required.

Install: `pip install -e .` then `python -m TinyCTX onboard`.

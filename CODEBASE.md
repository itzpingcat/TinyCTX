# CODEBASE.md — TinyCTX

> Auto-generated. Update this file when you make changes to the code.

## What TinyCTX Is

A context-efficient agentic assistant framework. You configure a language model, pick a bridge (CLI, Discord, or HTTP gateway), and get a persistent, tool-using AI agent with memory consolidation, scheduled heartbeats, subagent support, and web browsing.

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
│   ├── _instance.py     Shared instance-directory resolution (--dir / CWD .tinyctx / ~/.tinyctx)
│   ├── launch.py        tinyctx launch — attaches a bridge client
│   ├── start.py         tinyctx start  — docker compose up for the resolved instance
│   ├── stop.py          tinyctx stop   — docker compose down for the resolved instance
│   ├── status.py        tinyctx status
│   └── onboard.py       tinyctx onboard — delegates to onboard/
├── utils/
│   ├── tool_handler.py  ToolCallHandler — register/enable/execute tools
│   ├── commands.py      CommandRegistry — slash-command dispatch for bridges
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
│   └── anima/          generate_image_anima — always-on tool for Anima.json ComfyUI workflow
└── modules/            Auto-discovered plugins (see Module System below)
    ├── cron/           Cron scheduler
    ├── ctx_tools/      Context manipulation tools (edit, delete turns)
    ├── equipment_manifest/  Agent's self-description of available tools
    ├── filesystem/     view / write_file / edit_file / grep / glob_search tools
    ├── heartbeat/      Periodic agent turns on a background branch
    ├── mcp/            MCP server integration
    ├── memory/         Knowledge graph (LadybugDB property graph + librarian agents)
    ├── present/        present() tool — delivers files to users via bridges
    ├── rag/            Semantic search over workspace/memory/ (BM25 or embeddings)
    ├── shell/          shell tool
    ├── skills/         use_skill tool (loads SKILL.md files)
    ├── subagents/      spawn_agent / wait_agent tools
    ├── sysops/         User/permission management + /model command + set_active_model tool (per-branch LLM override, see below)
    ├── system_prompt/  Injects SOUL.md, AGENTS.md into system prompt
    ├── todo/           todo_read / todo_write tools (per-session task list)
    └── web/            web_search / open_url tools (DuckDuckGo + Playwright)
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

All bridges use an `asyncio.Queue` (`reply_queue`) passed to `Runtime.push()` to receive
events. `Runtime._process()` puts each event into the queue; the bridge's turn handler
drains it. A `None` sentinel signals the turn is complete.

---

## Key Contracts (`contracts.py`)

All cross-layer communication uses these frozen dataclasses/enums. No business logic lives here.

| Type | Purpose |
|------|---------|
| `Platform` | Enum: CLI, DISCORD, MATRIX, CRON, API, SYSTEM |
| `SessionEnvironment` | Environment context carried by every `InboundMessage`: `platform`, `agent_name`, `server_name`, `channel_name`. Constructed by bridges per message; snapshotted into `state_delta` by `Runtime`. Adding new session metadata goes here, not on `InboundMessage`. |
| `InboundMessage` | Canonical message envelope from bridges. Carries `tail_node_id`, `author` (User), `env` (SessionEnvironment), `text`, `attachments`, `trigger`. |
| `AgentTextChunk` | One streaming token |
| `AgentTextFinal` | End of turn (or non-streaming full text) |
| `AgentToolCall` | Tool invocation emitted during the tool loop |
| `AgentToolResult` | Tool result |
| `AgentError` | LLM error or cycle limit reached |
| `AgentOutboundFiles` | File paths to deliver to the user (from `present()` tool) |
| `AgentTextFinal.suppressed` | `True` when the agent's entire final reply was the literal sentinel `NO_REPLY` (see `agent.py`'s `NO_REPLY_TOKEN`) — bridges discard any buffered/streamed text and send nothing. Documented for the model in `modules/equipment_manifest/EM.md`. |
| `ToolCall` / `ToolResult` | Internal tool call/result; distinct from Agent* event types |
| `Attachment` | File attached to an inbound message |
| `IMAGE_BLOCK_PREFIX` | Sentinel prefix returned by filesystem view() for images |
| `MANUAL_LAUNCH_ATTR` | Module-level flag; bridges with this skip auto-start |

---

**Fixed bug (`/model` couldn't resolve caller identity):** `modules/sysops/__main__.py`'s `_resolve_model_caller()` read session state's `"author_id"` and passed it to `UserStore.get_by_platform(platform, author_id)`. But `"author_id"` in session state is the TinyCTX **username** (`runtime.py`'s `_compute_state_delta()` sets `mapping["author_id"] = msg.author.username`), while `get_by_platform` expects a platform-native user_id (Discord snowflake, etc.) as its second argument — a different key entirely, so the lookup essentially never matched and `/model` always reported "Cannot resolve your identity for this conversation." Fixed by looking the caller up via `runtime.users.get_user(author_id)` (username lookup) instead.

**Fixed bug (agent.db path mismatch):** `AgentCycle.run()` and `gateway.handle_lane_branch()` previously opened their own `ConversationDB` at `workspace/agent.db`, while `Runtime` (which writes every inbound user node) uses `data/agent.db`. Since `workspace/` and `data/` are different directories, this meant every cycle read/wrote an effectively empty, wrong-path SQLite file — any `add_node(parent_id=...)` referencing a node `Runtime.push()` had actually written failed `FOREIGN KEY constraint failed` (the parent row only existed in the other file). Fixed: `agent.py` now opens `data/agent.db` (same as `runtime.py` and `modules/memory/__main__.py`, which were already correct); `gateway/__main__.py`'s `handle_lane_branch` now reuses `request.app["runtime"].db` instead of opening a second connection at all.

## Database (`db.py`)

SQLite WAL-mode database at `<instance>/data/agent.db` (see Instance Layout below — NOT workspace/, so the agent's own filesystem tools never see it). All conversation state is a **tree of nodes** — every message is a node with a `parent_id`, forming branches.

**Key columns:** `id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id, attachment_paths, state_delta, flags`

**Session state** is reconstructed by walking the ancestor chain, merging `state_delta` JSON objects (most-recent wins). Checkpoint nodes with `"_checkpoint": true` stop the walk early. Keys written by `Runtime._compute_state_delta()`: `platform`, `author_id`, `agent_name`, `server_name`, `channel_name` — sourced from `msg.env` and `msg.author`.

**Flags** are a JSON array column (`flags TEXT`) on each node, used by modules to mark nodes without a dedicated column (e.g. `"librarian_visited"`).

Key methods:
- `add_node(parent_id, role, content, ...)` → `Node`
- `get_ancestors(node_id)` → `[Node]` root→tip order (excludes structural root)
- `load_session_state(node_id)` → `(dict, depth)` — reconstructs session state
- `get_state(node_id, key, default=None)` — single-key read via `load_session_state`
- `set_state(node_id, key, value)` — merge-write a single key onto `node_id`'s own `state_delta` (read-modify-write; safe alongside other modules writing other keys to the same node). Modules should use `get_state`/`set_state`, not `load_session_state`/`update_node_state_delta` directly — the latter is a blind full-column replace and two writers on the same node will clobber each other (fixed in `rag`, `skills`, `memory` modules, which previously did this).

**The `"model"` session-state key** is read by `AgentCycle.run()` itself (`agent.py`): `primary_name = state.get("model") or self.config.llm.primary`. Any branch-scoped override of the LLM used for a cycle goes through this one key. `modules/sysops/` is the only writer today — both `/model <name>` (slash command) and the agent-callable `set_active_model` tool set it via `db.set_state`; `/model clear` / `set_active_model("")` reset it to `""` so the `or` falls through to the config default. A module adding its own model-switching UI should write the same key rather than inventing a new one. Both live under `modules/sysops/`, gated at `permission_level >= model_min_permission` (default 75, `extra.sysops.model_min_permission` in config.yaml).
- `flag_branch(node_id, flag)` — walk ancestors, adding flag until one already has it
- `get_nodes_without_flag(flag)` — used by librarian to find unvisited nodes

---

## Context Assembly (`context.py`)

`Context` assembles a `list[dict]` (OpenAI message format) from:
1. Registered **prompt providers** (`register_prompt`) — each returns a string; all system-role providers are concatenated into one system message
2. **DB history** — `_load_from_db()` walks ancestor chain, deserialises content blocks
3. A **hook pipeline**:
   - `HOOK_PRE_ASSEMBLE_ASYNC` — awaited by AgentCycle *before* `assemble()`
   - `HOOK_PRE_ASSEMBLE` — sync, runs inside `assemble()`
   - `HOOK_FILTER_TURN` — `fn(entry, age, ctx) → bool` — drop turns
   - `HOOK_TRANSFORM_TURN` — `fn(entry, age, ctx) → HistoryEntry | None` — replace/compress turns
   - `HOOK_POST_ASSEMBLE` — `fn(messages, ctx) → list[dict] | None` — final reshape

User turns with `author_id` set are prefixed with `【author_id】: ` (fullwidth brackets, U+3010/U+3011) after the hook pipeline runs. Before prefixing, `_sanitize_brackets()` normalizes Unicode bracket look-alikes in the message content to ASCII, so this exact delimiter cannot be forged by user-supplied text.

**Diagnostic logging:** `assemble()` logs `logger.error` when a user entry with a non-None `parent_id` has `author_id=None` — this distinguishes real user nodes (which should always have an `author_id`) from synthetic image-relay turns (which are created by `add_tool_result` with no `parent_id` and no `author_id`, and are expected to have no prefix). Complementary logging in `runtime.push()` fires at node-write time if `msg.author.username` is empty.

After hook processing, adjacent same-role messages are merged. Then token budget enforcement trims oldest non-system turns until the count fits.

`assemble()` returns `(messages, AssembleMeta)` where `AssembleMeta` has `tokens_pre_trim`, `tokens_used`, `was_trimmed`.

---

## LLM Client (`ai.py`)

`LLM` — async OpenAI-compatible streaming client. Works with Anthropic (compat endpoint), OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp.

- `LLM.stream(messages, tools, priority=10)` yields: `TextDelta | ThinkingDelta | ToolCallAssembled | LLMError`
- Tool call argument fragments are assembled before yielding — callers always receive complete args dicts.
- Retries on `ClientConnectionError` (3 attempts, exponential backoff via tenacity).
- `budget_tokens` enables Anthropic extended thinking (forces `temperature=1`).
- `cache_prompts` injects `cache_control: ephemeral` on the last system message.

`Embedder` — async OpenAI-compatible embedding client. `embed(texts, priority=10)` batches automatically; `embed_one(text, priority=10)` is the single-string convenience wrapper.

### Priority queue

Every `LLM.stream()` / `Embedder.embed()` / `embed_one()` call is admission-controlled by a single module-level priority queue living inside `ai.py` itself — not a separate object, not passed around through `runtime.py`/`agent.py`/modules. Lower `priority` runs first; ties are FIFO (via a monotonic sequence counter, since `heapq` isn't stable on priority alone).

- `configure_parallel(n)` sets the max number of concurrent in-flight requests (from `config.parallel`, default 3). Called once at startup in `main.py` right after `config.load()`. Worker tasks spin up lazily on first use — no explicit start call needed anywhere else.
- Streaming stays live, not buffered: a queued request's generator hasn't started yet, so it emits nothing while waiting. The moment a worker admits it, the real `_stream_with_retry()` generator runs and each event is forwarded to the caller as it's produced — identical token-by-token behavior to a non-queued call, just gated on when it's allowed to start.
- A worker holds its slot for a stream's entire duration (submit → last chunk), not just the initial POST.
- Convention used by current call sites (not enforced, just a lower-is-more-urgent int): `0` for the user-facing main cycle (`agent.py`), `5` for query-time embeddings that block a tool call (`kg_search`, `rag_search`), `15` for librarian/dedup background agent loops, `20` for RAG indexer batch embedding (nobody waiting on it).
- This is also why `modules/memory/__main__.py` no longer has a `_YieldingLLM` wrapper — background librarian calls just pass `priority=15` and queue behind user turns naturally, instead of busy-polling `_user_cycles_active()` in a `stream()` wrapper. `_user_cycles_active()` itself still exists and is still used by `LibrarianRunner._poll_cycle()` to decide whether to *schedule* new background tasks at all (a separate concern from LLM-call ordering).

---

## Tool System (`utils/tool_handler.py`)

`ToolCallHandler`:
- `register_tool(fn, always_on=False, min_permission=25)` — introspects signature and docstring to build the JSON schema definition
- `enable(name)` — turns a tool on for the current cycle
- `tools_search(query)` — BM25 search over tool names+descriptions; enables matching tools; always-on tool exposed to the LLM
- `get_tool_definitions(caller_level, minimal_tokens)` — returns OpenAI-format tool definitions for enabled tools the caller has permission to use
- `execute_tool_call(tool_call, caller_level)` — dispatches sync or async functions; sync functions run in a thread-pool executor

**Known schema generation caveat:** `_python_type_to_json_schema` handles `from __future__ import annotations` by resolving bare stringified type names (`'list'`, `'bool'`, etc.) back to their builtins. Complex generic strings like `'list[str]'` are not parseable this way and fall back to `{"type": "string"}` — a wrong but non-crashing result. Bare `list` (resolved to the `list` builtin) is handled by the `origin is list or annotation is list` branch and always emits `{"type": "array", "items": {"type": "string"}}` to avoid producing a schema without `items`, which crashes llama.cpp/llama-swap's Jinja2 tool template.

Permission levels: 0–100. Each tool has a `min_permission`. `minimal_tokens=True` hides tools the caller can't use.

`apply_overrides(overrides)` — applies config-driven per-tool `always_on`/`min_permission` overrides (see `tool_overrides:` in Config, above) after all modules have registered their tools. `always_on=True` adds the tool to `enabled`, `False` removes it; `min_permission` is written straight onto the tool's registration dict. Fields left `None` on an override are no-ops.

---

## Module System (`module_registry.py`)

Modules live under `TinyCTX/modules/<name>/`. Auto-discovered if they have `__main__.py` or `__init__.py`.

Each module may expose:
- `register_runtime(runtime)` — called once at startup; build singletons, register slash commands, start background tasks
- `register_agent(cycle)` — called per `AgentCycle`; register tools, prompt providers, context hooks

Modules that only need per-cycle wiring skip `register_runtime`. Modules that only do startup work skip `register_agent`.

---

## User System (`users/`)

`User` — TinyCTX-internal user with a unique `username` (auto-generated if needed), `permission_level` (0–100), a list of `PlatformIdentity` objects (one per platform account), and a freeform `meta` dict.

`UserStore` — SQLite-backed. Takes a `data_dir` (an instance's `data/` path) explicitly from `Runtime`; falls back to `TINYCTX_DATA_PATH` env, then platformdirs, only when constructed without one (legacy/standalone callers). Hot path: `resolve_user(platform, user_id, username, display_name)` — lookup by `(platform, user_id)`, create if not found, update identity if changed. In-memory LRU cache on both `(platform, user_id)` and `username`.

Slash commands registered by `Runtime`:
- `/user grant <username> <level>` — requires caller level 100
- `/user info <username>`
- `/user rename <username> <new>`

---

## Runtime (`runtime.py`)

`Runtime` owns the shared resources and coordinates message processing:
- `db` — `ConversationDB` (shared write connection; AgentCycle opens its own for reading)
- `users` — `UserStore`
- `commands` — `CommandRegistry`
- `module_registry` — `ModuleRegistry`
- `_semaphore` — limits concurrent cycles (`max_workers`, default 8)

`push(InboundMessage, reply_queue)`:
1. Build content blocks from attachments
2. Compute `state_delta` from `msg.env` (platform, agent_name, server_name, channel_name) and `msg.author`; write user node to DB
3. If `msg.trigger`, spawn `_process()` as an asyncio task

`_process()` constructs an `AgentCycle`, runs it, and puts each event into `reply_queue`
(None sentinel on completion). Bridges pass a queue to `push()` and drain it themselves.

`abort(node_id)` — sets the abort event for a running cycle.

---

## Bridges

### CLI (`bridges/cli/__main__.py`)
- Sets `MANUAL_LAUNCH = True` — only starts via `tinyctx launch cli`
- Rich TUI with persistent session restore (cursor file at `<instance>/data/cursors/cli`, resolved via the `instance_dir` passed in by `commands/launch.py`)
- Supports paste refs, slash commands, copy helpers
- Provider presets for OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp, custom
- `agent_name` option: set `agent_name: "Aria"` under `bridges.cli.options` to stamp assistant nodes with a custom name (forwarded in every message payload to the gateway)

### Discord (`bridges/discord/`)

The Discord bridge is split across six modules:

```
bridges/discord/
  __main__.py   Thin entry point — instantiates DiscordBridge and calls run()
  bridge.py     DiscordBridge class — discord.py client setup, event routing
                (on_message / on_ready), access-control checks, attachment
                fetching, cursor wrappers, thread handling
  turn.py       handle_turn() + typing_keepalive() — drains the reply_queue,
                manages the typing indicator keepalive loop, chunks long replies
  commands.py   sync_app_commands() — builds Discord slash commands from
                CommandRegistry; handle_reset_interaction(),
                handle_shutdown_interaction(), handle_command_interaction()
  cursors.py    CursorStore — persists discord.json + discord_msg_nodes.json
                under data/cursors/ (bridge bookkeeping, not workspace/);
                make_session_node() helper
  compat.py     CompatRules — hot-reloads compat.json, matches messages against
                proxy-bot delay rules (e.g. Tupperbot)
  mentions.py   humanize_mentions() — <@id> → @username (inbound)
                dehumanize_mentions() — @username → <@id> (outbound)
  compat.json   Per-pattern delay rules (not a Python file)
```

Key config options (under `bridges.discord.options`):
- `token_env` — env var holding the bot token (default: `DISCORD_BOT_TOKEN`)
- `allowed_users_dm` — allowlist of user IDs for DMs (empty = open)
- `allowed_servers` — map of guild ID → list of channel IDs (empty list = all channels)
- `admin_users` — user IDs permitted to use `/reset` and `/shutdown` in groups
- `prefix_required` — only respond when @mentioned or message starts with `command_prefix`
- `command_prefix` — trigger prefix for group channels (default: `!`)
- `reset_command` / `shutdown_command` — slash command names
- `max_reply_length` — Discord message chunk size cap (default: 1900)
- `typing_indicator` / `typing_on_thinking` / `typing_on_tools` / `typing_on_reply`

`agent_name` is populated automatically per message via `_bot_display_name(guild)`, which uses the bot's server nickname when set (so the bot can have different names in different guilds) and falls back to its global display name. It flows into session state via `SessionEnvironment` so the memory librarian sees the correct name on assistant nodes.

Thread branching: when a thread is created inside a tracked channel, the bot forks a
new DB branch from the channel turn that spawned it. Both evolve independently.
Cursors (`dm:<uid>`, `group:<cid>`, `thread:<tid>`) are persisted in
`<instance>/data/cursors/discord.json` so sessions survive restarts.

### Gateway (`gateway/__main__.py`)
- aiohttp HTTP server exposing `/v1/chat` (OpenAI-compat SSE)
- `api_key` authentication
- Also exposes `/v1/health`

---

## Notable Modules

### `system_prompt` — injects SOUL.md, AGENTS.md,, TOOLS.md into every system prompt via `register_prompt` providers.

### `rag` — indexes `workspace/memory/*.md` files; auto-injects relevant chunks each turn (BM25 or embedding cosine similarity); provides `memory_search` tool; triggers background memory consolidation when context budget is near.

### `memory` — LadybugDB property-graph knowledge store, stored at `<instance>/data/memory/graph.lbug` (not workspace/). A background "librarian" walks unvisited conversation nodes (tracked with DB flags), extracts entities/relationships via sub-agents, and writes to the graph. Main agent uses `kg_search` / `kg_traverse` / `call_librarian` tools. Pinned entities are injected into the system prompt. The librarian identifies the agent by reading `author_id` on assistant nodes (set from session state `agent_name`); this is how it knows which speaker is the agent vs the user in the conversation transcript. Conversation excerpts passed to the buffer agent are rendered by `nodes_to_text()` (`librarian_agents.py`) as `【author】: content` lines (fullwidth brackets, matching `context.py`'s convention); content is passed through `_sanitize_brackets()` first so it cannot forge this delimiter. Deduplication's `dedup_cache.db` also lives under `data/` (`dedup_agents.py`'s `run_dedup_cycle` takes `data_path`, not a workspace path).

### `heartbeat` — fires periodic agent turns on a background DB branch at a configured interval. Suppresses `NO_REPLY` replies (same sentinel as `agent.py`'s `NO_REPLY_TOKEN` — unified single marker, was `HEARTBEAT_OK`). Slash command: `/heartbeat run`.

### `cron` — CRON.json-backed job scheduler; creates agent turns at specified times.

### `filesystem` — `view`, `write_file`, `edit_file`, `grep`, `glob_search` tools. Write tools require prior `view()` to prevent blind overwrites. `write_file`/`edit_file` are sandboxed to `workspace/` only. `view`/`grep`/`glob_search` can additionally reach any directory listed in `filesystem.read_only_paths` in config.yaml (e.g. `/app` for the agent's own source in the container) — read-only, never writable; nothing outside workspace/ + that whitelist is reachable at all. `view()` detects image files by extension (`.jpg/.jpeg/.png/.gif/.webp`) and returns them as `IMAGE_BLOCK_PREFIX` sentinels; `agent._execute_tool` unwraps these, converts to PNG via Pillow, and injects a follow-up user turn with an `image_url` block. If PNG conversion fails (Pillow unavailable), the image path falls back to a plain text description rather than sending an unconverted format to the backend (which would cause a misleading "mmproj is missing" backend error).

### `shell` — `shell` tool. Runs in workspace directory. Maintains a blacklist of dangerous commands.

### `web` — `web_search` (DuckDuckGo via `ddgs`) and `open_url` (Playwright, headless by default; `headless=False` for captchas).

### `subagents` — `spawn_agent(prompt)` and `wait_agent(task_id)` for parallel side tasks on child branches.

### `skills` — `use_skill(name)` tool. Loads `SKILL.md` from `workspace/skills/<name>/`. Follows agentskills.io convention.

### `todo` — `todo_read` / `todo_write`. Session-scoped task checklist.

### `present` — `present(paths)` tool. Emits `AgentOutboundFiles` events that bridges turn into file attachments.

### `mcp` — MCP server integration; loads configured MCP servers and registers their tools into the cycle.

---

## Config (`config/`)

YAML-based. Loaded from `<instance>/config.yaml` by default (see Instance Layout below), or a path passed explicitly via `--config`. Key top-level keys:

- `workspace.path` — default `<instance>/workspace` (i.e. config.yaml's own directory + `/workspace`) — no longer needs stating explicitly in most configs
- `data.path` — default `<instance>/data` — internal state (agent.db, users.db, memory graph, cursors); the agent's filesystem tools never see this directory
- `models` — dict of named model configs (`kind`, `base_url`, `api_key_env`, `model`, `max_tokens`, `temperature`, `supports_vision`, `tokens_per_image`)
- `llm.primary` / `llm.fallback` — model name(s); AgentCycle tries primary then fallbacks
- `context` — token budget for context assembly
- `max_tool_cycles` — max tool-call iterations per turn
- `parallel` — max concurrent in-flight LLM/embedding requests across the whole process (default 3); see Priority queue under `ai.py` above
- `bridges.<name>.enabled` / `bridges.<name>.options` — per-bridge config
- `gateway.enabled` / `gateway.host` / `gateway.port` / `gateway.api_key` (`gateway.port` can be overridden per-instance via `TINYCTX_PORT` env, injected by `tinyctx start`)
- `logging.level`
- `permissions.minimal_tokens` — hide tools from LLM that the caller can't use
- `tool_overrides` — dict of `<tool_name>: {always_on?, min_permission?}`, applied once per `AgentCycle.run()` right after `module_registry.register_agent(self)` (so it wins over whatever each module's `register_tool()` call set). Unknown tool names are skipped with a debug log (not every module is loaded in every config). Parsed into `Config.tool_overrides: dict[str, ToolOverrideConfig]`; applied via `ToolCallHandler.apply_overrides()`.

---

## Instance Layout (`commands/_instance.py`)

An *instance* is a self-contained directory holding one agent's config, workspace, and internal data. Resolved by every CLI command the same way: `--dir` flag → nearest ancestor of CWD literally named `.tinyctx` → `.tinyctx/` child of CWD → `~/.tinyctx`. This is what makes multiple concurrent agents possible — each just needs its own instance directory.

```
<instance>/                 e.g. ~/.tinyctx, or anywhere via --dir
├── config.yaml             Loaded by default from here (workspace.path / data.path default relative to this file)
├── .env                     Optional. KEY=VALUE per line (e.g. DISCORD_BOT_TOKEN=...). Loaded via
│                            `commands/_instance.py`'s `load_instance_env()` — with override=True, so
│                            values here win over anything already exported in the shell/global env.
│                            Loaded by `main.py` (direct/non-Docker launch) and `commands/start.py`
│                            (Docker launch — populates the host process env before `docker compose up`,
│                            which compose.yaml's bare `environment:` entries then pass into the container).
├── workspace/               Agent-authored content — visible to the agent's own filesystem tools
│   ├── SOUL.md              Agent personality (loaded every turn)
│   ├── AGENTS.md            Sub-agent/persona definitions
│   ├── CRON.json            Scheduled jobs
│   ├── HEARTBEAT.md         Heartbeat instructions
│   ├── downloads/           Files/images sent by users via bridges
│   ├── uploads/             Large attachments saved instead of inlined
│   ├── skills/<name>/SKILL.md
│   └── rag/, memory/*.md    Semantic search corpus (RAG module; distinct from the data/memory/ graph below)
└── data/                     TinyCTX-internal state — NOT visible to the agent's filesystem tools
    ├── agent.db              Conversation tree (SQLite)
    ├── users.db              UserStore
    ├── cursors/               Per-bridge session cursors (discord.json, discord_msg_nodes.json, cli)
    └── memory/                LadybugDB graph (graph.lbug), librarian.log, dedup_cache.db
```

Docker Compose (`compose.yaml`, always at the repo root, shared across instances) is invoked with `-f <repo>/compose.yaml -p <project>` plus env vars (`TINYCTX_CONFIG_FILE`, `TINYCTX_WORKSPACE`, `TINYCTX_DATA`, `TINYCTX_PORT`, `TINYCTX_INSTANCE`, `TINYCTX_TAG`) computed by `commands/_instance.py` from the resolved instance dir — see `compose_env()`. `TINYCTX_TAG` is a separate, short (6 hex char) hash from `TINYCTX_INSTANCE` because Docker bridge interface names are capped at 15 chars (`IFNAMSIZ`) on Linux.

Non-Docker launches (`onboard`'s direct `python main.py` spawn) instead set `TINYCTX_CONFIG_FILE` in the subprocess env; `main.py` reads it if present, else defaults to `config.yaml` relative to CWD.

---

## Dependency Notes

Key packages: `aiohttp`, `rich`, `questionary`, `mcp`, `tiktoken`, `structlog`, `tenacity`, `ddgs`, `playwright`, `pdfplumber`, `python-docx`, `croniter`, `python-dotenv`, `discord.py`, `jinja2`, `numpy`.

Python ≥ 3.14 required.

Install: `pip install -e .` then `python -m TinyCTX onboard`.

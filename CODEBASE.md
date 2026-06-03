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
├── module_registry.py  Loads modules from modules/ and wires them into each AgentCycle
│
├── config/             Config loading (YAML → dataclasses)
├── users/              UserStore + User/PlatformIdentity models (SQLite)
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
├── commands/
│   ├── launch.py   tinyctx launch — attaches a bridge client
│   ├── start.py    tinyctx start  — daemonises main.py
│   ├── stop.py     tinyctx stop
│   ├── status.py   tinyctx status
│   └── onboard.py  tinyctx onboard — delegates to onboard/
│
├── onboard/            Interactive first-run setup wizard
│   ├── __main__.py     Orchestrates setup steps
│   ├── providers_setup.py
│   ├── gateway_setup.py
│   ├── bridges_setup.py
│   └── workspace_setup.py
│
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
    ├── sysops/         System operation tools (model switching, abort, etc.)
    ├── system_prompt/  Injects SOUL.md, AGENTS.md, MEMORY.md into system prompt
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
| `InboundMessage` | Canonical message envelope from bridges. Carries `tail_node_id`, `author` (User), `text`, `attachments`, `trigger`. |
| `AgentTextChunk` | One streaming token |
| `AgentTextFinal` | End of turn (or non-streaming full text) |
| `AgentToolCall` | Tool invocation emitted during the tool loop |
| `AgentToolResult` | Tool result |
| `AgentError` | LLM error or cycle limit reached |
| `AgentOutboundFiles` | File paths to deliver to the user (from `present()` tool) |
| `ToolCall` / `ToolResult` | Internal tool call/result; distinct from Agent* event types |
| `Attachment` | File attached to an inbound message |
| `IMAGE_BLOCK_PREFIX` | Sentinel prefix returned by filesystem view() for images |
| `MANUAL_LAUNCH_ATTR` | Module-level flag; bridges with this skip auto-start |

---

## Database (`db.py`)

SQLite WAL-mode database at `workspace/agent.db`. All conversation state is a **tree of nodes** — every message is a node with a `parent_id`, forming branches.

**Key columns:** `id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id, author_name, attachment_paths, state_delta, flags`

**Session state** is reconstructed by walking the ancestor chain, merging `state_delta` JSON objects (most-recent wins). Checkpoint nodes with `"_checkpoint": true` stop the walk early.

**Flags** are a JSON array column (`flags TEXT`) on each node, used by modules to mark nodes without a dedicated column (e.g. `"librarian_visited"`).

Key methods:
- `add_node(parent_id, role, content, ...)` → `Node`
- `get_ancestors(node_id)` → `[Node]` root→tip order (excludes structural root)
- `load_session_state(node_id)` → `(dict, depth)` — reconstructs session state
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

After hook processing, adjacent same-role messages are merged. Then token budget enforcement trims oldest non-system turns until the count fits.

`assemble()` returns `(messages, AssembleMeta)` where `AssembleMeta` has `tokens_pre_trim`, `tokens_used`, `was_trimmed`.

---

## LLM Client (`ai.py`)

`LLM` — async OpenAI-compatible streaming client. Works with Anthropic (compat endpoint), OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp.

- `LLM.stream(messages, tools)` yields: `TextDelta | ThinkingDelta | ToolCallAssembled | LLMError`
- Tool call argument fragments are assembled before yielding — callers always receive complete args dicts.
- Retries on `ClientConnectionError` (3 attempts, exponential backoff via tenacity).
- `budget_tokens` enables Anthropic extended thinking (forces `temperature=1`).
- `cache_prompts` injects `cache_control: ephemeral` on the last system message.

`Embedder` — async OpenAI-compatible embedding client. `embed(texts)` batches automatically.

---

## Tool System (`utils/tool_handler.py`)

`ToolCallHandler`:
- `register_tool(fn, always_on=False, min_permission=25)` — introspects signature and docstring to build the JSON schema definition
- `enable(name)` — turns a tool on for the current cycle
- `tools_search(query)` — BM25 search over tool names+descriptions; enables matching tools; always-on tool exposed to the LLM
- `get_tool_definitions(caller_level, minimal_tokens)` — returns OpenAI-format tool definitions for enabled tools the caller has permission to use
- `execute_tool_call(tool_call, caller_level)` — dispatches sync or async functions; sync functions run in a thread-pool executor

Permission levels: 0–100. Each tool has a `min_permission`. `minimal_tokens=True` hides tools the caller can't use.

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

`UserStore` — SQLite-backed (`~/.config/tinyctx/users.db` or `$TINYCTX_CONFIG_DIR/users.db`). Hot path: `resolve_user(platform, user_id, username, display_name)` — lookup by `(platform, user_id)`, create if not found, update identity if changed. In-memory LRU cache on both `(platform, user_id)` and `username`.

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
2. Write user node to DB with `state_delta`
3. If `msg.trigger`, spawn `_process()` as an asyncio task

`_process()` constructs an `AgentCycle`, runs it, and puts each event into `reply_queue`
(None sentinel on completion). Bridges pass a queue to `push()` and drain it themselves.

`abort(node_id)` — sets the abort event for a running cycle.

---

## Bridges

### CLI (`bridges/cli/__main__.py`)
- Sets `MANUAL_LAUNCH = True` — only starts via `tinyctx launch cli`
- Rich TUI with persistent session restore (reads cursor from `workspace/cursors/`)
- Supports paste refs, slash commands, copy helpers
- Provider presets for OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp, custom

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
                under workspace/cursors/; make_session_node() helper
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

Thread branching: when a thread is created inside a tracked channel, the bot forks a
new DB branch from the channel turn that spawned it. Both evolve independently.
Cursors (`dm:<uid>`, `group:<cid>`, `thread:<tid>`) are persisted in
`workspace/cursors/discord.json` so sessions survive restarts.

### Gateway (`gateway/__main__.py`)
- aiohttp HTTP server exposing `/v1/chat` (OpenAI-compat SSE)
- `api_key` authentication
- Also exposes `/v1/health`

---

## Notable Modules

### `system_prompt` — injects SOUL.md, AGENTS.md, MEMORY.md, TOOLS.md into every system prompt via `register_prompt` providers.

### `rag` — indexes `workspace/memory/*.md` files; auto-injects relevant chunks each turn (BM25 or embedding cosine similarity); provides `memory_search` tool; triggers background memory consolidation when context budget is near.

### `memory` — LadybugDB property-graph knowledge store. A background "librarian" walks unvisited conversation nodes (tracked with DB flags), extracts entities/relationships via sub-agents, and writes to the graph. Main agent uses `kg_search` / `kg_traverse` / `call_librarian` tools. Pinned entities are injected into the system prompt.

### `heartbeat` — fires periodic agent turns on a background DB branch at a configured interval. Suppresses `HEARTBEAT_OK` replies. Slash command: `/heartbeat run`.

### `cron` — CRON.json-backed job scheduler; creates agent turns at specified times.

### `filesystem` — `view`, `write_file`, `edit_file`, `grep`, `glob_search` tools. Write tools require prior `view()` to prevent blind overwrites. Operations sandboxed to workspace directory.

### `shell` — `shell` tool. Runs in workspace directory. Maintains a blacklist of dangerous commands.

### `web` — `web_search` (DuckDuckGo via `ddgs`) and `open_url` (Playwright, headless by default; `headless=False` for captchas).

### `subagents` — `spawn_agent(prompt)` and `wait_agent(task_id)` for parallel side tasks on child branches.

### `skills` — `use_skill(name)` tool. Loads `SKILL.md` from `workspace/skills/<name>/`. Follows agentskills.io convention.

### `todo` — `todo_read` / `todo_write`. Session-scoped task checklist.

### `present` — `present(paths)` tool. Emits `AgentOutboundFiles` events that bridges turn into file attachments.

### `mcp` — MCP server integration; loads configured MCP servers and registers their tools into the cycle.

---

## Config (`config/`)

YAML-based. Loaded from `config.yaml` (or path specified via `--config`). Key top-level keys:

- `workspace.path` — default `~/.tinyctx`
- `models` — dict of named model configs (`kind`, `base_url`, `api_key_env`, `model`, `max_tokens`, `temperature`, `supports_vision`, `tokens_per_image`)
- `llm.primary` / `llm.fallback` — model name(s); AgentCycle tries primary then fallbacks
- `context` — token budget for context assembly
- `max_tool_cycles` — max tool-call iterations per turn
- `bridges.<name>.enabled` / `bridges.<name>.options` — per-bridge config
- `gateway.enabled` / `gateway.host` / `gateway.port` / `gateway.api_key`
- `logging.level`
- `permissions.minimal_tokens` — hide tools from LLM that the caller can't use

---

## Workspace Layout (`~/.tinyctx/`)

```
agent.db          Conversation tree (SQLite)
cursors/          Per-bridge session cursors (CLI resume)
SOUL.md           Agent personality (loaded every turn)
AGENTS.md         Sub-agent/persona definitions
MEMORY.md         Long-term facts always in context
memory/           Semantic search corpus (*.md files, subdirs OK)
downloads/        Files/images sent by users via bridges
CRON.json         Scheduled jobs
HEARTBEAT.md      Heartbeat instructions (read by agent via filesystem tools)
skills/
  <name>/
    SKILL.md
```

---

## Dependency Notes

Key packages: `aiohttp`, `rich`, `questionary`, `mcp`, `tiktoken`, `structlog`, `tenacity`, `ddgs`, `playwright`, `pdfplumber`, `python-docx`, `croniter`, `discord.py`, `jinja2`, `numpy`.

Python ≥ 3.11 required.

Install: `pip install -e .` then `python -m TinyCTX onboard`.

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
│   ├── launch.py        tinyctx launch — attaches a bridge client
│   ├── start.py         tinyctx start  — docker compose up for the resolved instance
│   ├── stop.py          tinyctx stop   — docker compose down for the resolved instance
│   ├── status.py        tinyctx status
│   └── onboard.py       tinyctx onboard — delegates to onboard/
├── utils/
│   ├── instance.py      Shared instance-directory resolution (--dir / CWD .tinyctx / ~/.tinyctx)
│   ├── tool_handler.py  ToolCallHandler — register/enable/execute tools
│   ├── commands.py      CommandRegistry — slash-command dispatch for bridges
│                        Handlers reply via EITHER `await context["send"](text)`
│                        OR `context["console"].print(text)` — a bridge MUST
│                        supply both keys or send-style handlers go silent.
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

**Per-model context budget:** `ModelConfig.context` (config/`__main__.py`, default `16384`) is a per-model token budget for conversation history. `agent.py`'s cycle setup wires the *primary* model's `context` value into `Context(token_limit=...)`, which `context.py`'s trim loop enforces directly. There is no `n_ctx` hint sent to the backend — llama.cpp/llama-swap-style servers must be sized/configured on their own end; TinyCTX no longer sends a suggested context window (this `n_ctx`/`context_overhead` mechanism was removed as dead weight — it never actually constrained what got sent, only advised the backend, and the backend's own config is the real source of truth for its context window).

**Token-count fuzz factor:** `Config.token_fuzz` (default `1.1`) is a global multiplier applied to every counted-token total in `Context._count_tokens` (`context.py`), to pad for tokenizer estimation error. Configurable via top-level `token_fuzz:` in config.yaml.

`Embedder` — async OpenAI-compatible embedding client. `embed_one` is removed — `embed(texts, priority=10, kind="default")` is now the only entry point; callers hand it the full list (even a single item as a one-element list) and it chunks into `batch_size`-sized requests internally. If a whole batch's API call fails, it falls back to embedding that batch's texts individually — those per-item retries run concurrently via `asyncio.gather`, not sequentially, since a `_queue_worker` holds its slot for `embed()`'s entire duration (see Priority queue below) and retrying `batch_size` items one at a time would tie that slot up for `batch_size` extra sequential round trips. An item that still fails on its own comes back as `None` in its slot instead of raising. The outer loop across multiple `batch_size` chunks (for a `texts` list larger than one batch) is still sequential, not concurrent — parallelizing that would let one worker fire many more concurrent HTTP requests than `configure_parallel`'s cap intends. `kind` picks the template: `"query"` → `query_template`, `"document"` → `document_template`, anything else (including the `"default"` default) → no templating (raw text). Callers that cannot tolerate a partial result (e.g. `modules/rag/indexer.py`'s `_index_file()`, which must not mark a file "indexed" when a chunk's embedding actually failed) must check the returned list for `None` themselves and raise/abort — `embed()` itself no longer does that for them.

### Priority queue

Every `LLM.stream()` / `Embedder.embed()` call is admission-controlled by a single module-level priority queue living inside `ai.py` itself — not a separate object, not passed around through `runtime.py`/`agent.py`/modules. Lower `priority` runs first; ties are FIFO (via a monotonic sequence counter, since `heapq` isn't stable on priority alone).

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
- Session start: every launch branches fresh off root (`/v1/lane/branch` → `/v1/lane/open`) so a new session doesn't inherit the last one's context. The pre-launch cursor is held in `_resume_cursor`; `/resume` reattaches to it. Set `default_to_resume: true` under `bridges.cli.options` to reattach automatically instead of branching.
- Streaming render (`_split_blocks` / `_emit_text` / `_print_block`): **append-only — nothing on screen is ever repainted.** Streamed text is buffered and flushed one markdown block at a time, at block boundaries (blank line outside a fence, or a closing fence). Each block is captured, its surrounding blank lines trimmed, and separated from the next by exactly one blank line.
  - This replaced a `rich.live.Live` region. Live repaints by moving the cursor up over its own output, which broke two ways: a render taller than the terminal couldn't be scrolled back over, so every refresh reprinted the whole reply (duplicated-message bug); and scrolling mid-reply desynced Rich's cursor position from the real one, smearing output.
  - **Do not reintroduce `Live` or anything else that repositions the cursor here.** The renderer emits zero cursor-movement escapes; that property is what makes mid-stream scrolling safe.

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

### `rag` — indexes named databank folders under `workspace/rag/` (BM25 or embedding cosine similarity via `rag_search`/`set_auto_rag_databanks` tools). Any `*.md` file may open with a YAML frontmatter header (`modules/rag/lorefile.py`: `name`, `mode`, `keys`, `secondary_keys`, `constant`, `selective`, `selective_logic`, `case_sensitive`, `whole_words`, `disabled`) declaring it a keyword-triggered lore entry — files with no frontmatter are indexed as plain text, default to `mode: hybrid`, and never keyword-trigger (no keys), unchanged from before. Frontmatter is stripped before chunking so only the body is embedded/BM25-indexed (prefixed with name+keys for recall). Legacy SillyTavern lorebook/worldinfo JSON dropped at the root of a databank dir is auto-converted the first time it's discovered — `discover_databanks()` writes one native lore `.md` doc per active entry into a same-named folder (`lorefile.convert_lorebook_json`), then renames the original to `<name>.json.bak` (never deletes) so it's never re-converted; from then on it's an ordinary folder databank, no JSON support at runtime. `FilesDataBank` (`modules/rag/databanks.py`) is the only databank kind — `LoreBookDataBank` no longer exists, and there's no separate `DataBank` protocol either; callers type against `FilesDataBank` directly.

**Auto-inject is both keyword-triggered and semantic, gated per-entry by `mode`.** `FilesDataBank.auto_inject(text, store=None, embedder=None, top_k=0, bm25_weight=0.3)` is `async`: it runs ST-style keyword/regex matching (ported selectiveLogic AND_ANY/NOT_ALL/NOT_ANY/AND_ALL) over frontmatter entries deterministically, and when a `store` is passed it additionally runs the same hybrid BM25+vector search `rag_search` uses over the whole databank index, merging in new hits deduplicated by resolved file path. Each entry's frontmatter `mode` (`lorefile.LoreEntry.mode`, default `"hybrid"`) decides which half applies to it:
  - `hybrid` (default) — deterministic firing AND eligible for the passive semantic merge.
  - `vector` — semantic-only; skips keyword/regex matching entirely regardless of `keys`/`constant`.
  - `keyword` — deterministic-only via literal substring/whole-word `keys`; excluded from the semantic merge so it never surfaces passively without a keyword hit.
  - `regex` — like `keyword`, but `keys` (and `secondary_keys`) are compiled as regular expressions (`lorefile.compile_regex_key` — accepts SillyTavern's `/pattern/flags` syntax, or a bare pattern) instead of literal substrings; also excluded from the semantic merge.

  `rag_search` (the explicit tool) is unaffected by `mode` — it always searches the full hybrid index regardless. Legacy lorebook conversion (`convert_lorebook_json`) auto-assigns `mode: regex` when any of an entry's keys use ST's `/pattern/flags` syntax, else `mode: keyword` (converted entries never default to `hybrid`/`vector` since legacy lorebooks have no concept of embeddings — edit the converted `.md` by hand to opt in). The pre-assemble hook in `modules/rag/__main__.py` syncs each auto-rag target's indexer before calling `auto_inject`, same as `rag_search` does.

**`FilesDataBank` caches parsed entries by `(path, mtime)`** (`_entry_cache` in `databanks.py`) so `iter_files()`/`auto_inject()` — the latter invoked once per user turn per auto-rag target — don't re-read and re-parse every file in the folder when nothing on disk changed; stale cache entries are dropped when a file disappears or falls outside the indexed extensions.

**Auto-rag targets have a configurable default.** `default_auto_targets` (list of databank names, default `[]`) in `EXTENSION_META`'s `default_config` is used by the pre-assemble hook when a branch's `rag_auto_targets` session-state key was never written (`get_state` returns `None`) — once `set_auto_rag_databanks` is called on a branch, even with `[]` to explicitly clear it, that stored value wins over the config default from then on.

**Module state in `modules/rag/__main__.py`** is one `_RagState` dataclass instance (`_state`) instead of eight separate module globals (`initialized`, `stores`, `indexers`, `databanks`, `embedder`, `cfg`, `workspace`, `strategy`, `model_name`).

### `memory` (v2) — scoped LadybugDB property-graph knowledge store at `<instance>/data/memory/memory.lbug` (not workspace/). See `modules/memory/PLAN.md` for the full design.

**Schema.** `Entity(uuid, name, entity_type, description, scope, pinned, mention, created_at, updated_at, embed_hash, embed_content, embedding)` and `Relation(relation, weight, created_at, updated_at)`. The old `priority` field, the second `graph_*` embedding model, and the `superseded_at` soft-delete column are all removed. `mention` is a DOUBLE (passive RAG bumps +0.1, `search_memory` +1.0) and, with `created_at`/`updated_at`, is agent-read-only. `GraphMeta.schema_version = "2"`.

**Scoping** (`scopes.py`) is information isolation, not ownership — most nodes are `global`. Grammar `global` | `kind:target` (`user:bob`, `guild:my_server`), reused by the `pinned` field. `resolve_scopes(env, active_users)` computes the per-cycle visible set (global + guild + recent participants); enforcement is at the **query layer** — every read filters `WHERE e.scope IN visible` and an edge is visible only if both endpoints are. The scope set is carried per-task in a `contextvars.ContextVar` (`tools.scope_context`), so concurrent librarians with different scopes don't collide.

**Vector index** (`graph.py VectorIndex`) is an in-memory matrix cache invalidated by a dirty set the writers populate (embed_hash zeroed → removed from index; the embedding pass re-adds). `search()` applies **min-p before top-k** and restricts to an `allowed` uuid set. Cosine via numpy with a pure-Python fallback.

**Tools** (`tools.py`, single file). Main agent: `search_memory` (exact-match short-circuit, else hybrid BM25+vector with **min-p before RRF**), `memory_stats` (scope-filtered + reviewer backlog), `call_librarian`. Librarians additionally get `memory_add_entity` (atomic unique-name-in-scope; returns existing node data on collision), `memory_update_entity_description` (unified-diff apply; distinguishes malformed vs stale-base), `memory_set_entity_pinned`, `memory_set_entity_scope`, `memory_delete_entity`, `memory_set_relationship` (SCREAMING_SNAKE_CASE validated; `/`-groups in `prompts/default_relations.txt` are mutually-exclusive within an ordered pair), `memory_delete_relationship`, `memory_merge_into` (duplicate/alias; shares an internal helper with the deduper).

**Librarians.** `extractor.py` ingests unvisited conversation branches within a resolved write scope (defaults new nodes to `global`, narrows only for sensitive info). `reviewer.py` loads dynamically-registered flaggers from `flaggers/` (orphaned, description_length, too_many_edges, over_pinned, decay_candidate, fuzzy_names), maintaining a **persisted** de-duplicated issue queue (`data/reviewer_queue.json`, key `(flagger_type, sorted(uuids))`, survives restart) drained with an adaptive throttle. `deduper.py` runs the embedding pass plus semantic dedup (candidate pairs → greedy clique-edge-cover → LLM verify → merge; distinct pairs cached in `data/dedup_cache.db`). Shared agent-loop/tool-handler/sanitized `nodes_to_text` plumbing is in `librarian_common.py`. The `【author】: content` rendering + `sanitize_brackets` injection defense and the "never extract from the assistant's own turns" rule are preserved.

**Decay** is no longer an auto-deleter: the `decay_candidate` flagger surfaces stale/quiet/isolated nodes for the Reviewer to *assess* (absolute thresholds, never population-relative), so quiet-but-important data is never mechanically destroyed.

**Migration** (`migrate.py`): one-shot, runs when `graph.lbug` exists and `memory.lbug` doesn't; maps v1→v2 (everything → `global` scope, `pinned_target`→`pinned`, `mention_count`→`mention`, drops `priority`/`graph_*`, skips superseded edges, preserves embeddings only when the hash matches else lazy re-embed), verifies counts, then **renames** the old file to `.migrated.bak` (never deletes; `--purge` removes the backup, `--dry-run` writes nothing).

Superseded modules `decay.py` / `dedup_agents.py` / `librarian_agents.py` are inert deprecation stubs (file deletion was unavailable in the authoring environment). Tests: `tests/test_memory.py` (16 pure-logic + fake-backed scope-isolation tests; live-DB paths need a ladybug + py3.14 environment).

### `heartbeat` — fires periodic agent turns on a background DB branch at a configured interval. Suppresses `NO_REPLY` replies (same sentinel as `agent.py`'s `NO_REPLY_TOKEN` — unified single marker, was `HEARTBEAT_OK`). Slash command: `/heartbeat run`.

### `cron` — CRON.json-backed job scheduler; creates agent turns at specified times.

### `filesystem` — `view`, `write_file`, `edit_file`, `grep`, `glob_search` tools. Write tools require prior `view()` to prevent blind overwrites. `write_file`/`edit_file` are sandboxed to `workspace/` only. `view`/`grep`/`glob_search` can additionally reach any directory listed in `filesystem.read_only_paths` in config.yaml (e.g. `/app` for the agent's own source in the container) — read-only, never writable; nothing outside workspace/ + that whitelist is reachable at all. `view()` detects image files by extension (`.jpg/.jpeg/.png/.gif/.webp`) and returns them as `IMAGE_BLOCK_PREFIX` sentinels; `agent._execute_tool` unwraps these, converts to PNG via Pillow, and injects a follow-up user turn with an `image_url` block. If PNG conversion fails (Pillow unavailable), the image path falls back to a plain text description rather than sending an unconverted format to the backend (which would cause a misleading "mmproj is missing" backend error).

### `shell` — `shell` tool. Runs in workspace directory. Linux only (PowerShell support and `_normalize_windows` were removed with the container refactor).

**Command policy is AST-based (v2).** `validate.py` parses the command with `tree-sitter-bash` and checks each *resolved command* in it separately; `policy.py` compiles the rules from YAML. This replaced substring-glob `blacklist.txt` / `whitelist.txt`, which could not distinguish a command from a quoted argument containing the same text — `echo "i"; echo "am"; echo "harmless"` was blocked, and `git commit -m "don't rm -rf your repo"` was too. Design of record: `modules/shell/PLAN.md`.

- Two policy files, both shipped in the module, overridable via `extra.shell.policy.{deny,allow}` (typically pointed at the instance dir, mounted read-only): `deny.yaml` (`default_action: allow` — deny-list, for callers at/above `neutral`) and `allow.yaml` (`default_action: deny` — allow-list with per-command `subcommand`/`allowed_flags`/`max_args`/`arg_matches` constraints, for callers below `neutral`). Deny beats allow. Loaded once, cached by `(path, workspace)`.
- Rules match on parsed structure: `command` (basename), `subcommand`, `any_flag`/`all_flags`, `no_operands`, `path_under`/`path_outside`/`redirect_under`. Flags are normalized, so `-rf`, `-fr` and `-r -f` are one rule. `action: warn` replaced the old `_DESTRUCTIVE` regex list — warnings run and prefix the output.
- Each YAML also carries a `constructs:` map of permitted bash node types. **Anything not listed is denied**, so a `tree-sitter-bash` grammar upgrade fails closed; hence the version pin in `pyproject.toml`. `function_definition` is denied at every tier (rebinding `rm()` would defeat a name-based rule). At the allow-list tier substitutions, expansions, redirects and backgrounding are all denied, which is what makes injection structurally impossible and retires the old `{arg}` character-class hack — quoted text is a parser leaf, so callers get their metacharacters back as data.
- **Fails closed:** a missing/malformed policy blocks every command (the old `blacklist.txt` did the opposite — missing file meant unrestricted). Not configurable. Also denied: commands whose name isn't a literal word (`$CMD`, `$(echo rm)`), and any input with a parse error.
- **Explicitly out of scope:** obfuscation and interpreter escape. No recursive re-parsing of `bash -c` payloads, no base64 decoding. Flat rules catch the unsubtle cases (`bash -c`, `python -c`, pipe-to-shell via `no_operands`); nothing more is claimed. Backgrounding (`&`) is allowed at `neutral` so the agent can start long jobs. The container is the security boundary.
- Four permission levels gate access, configured via `extra.shell.permissions` (defaults: `use_whitelist=10`, `neutral=45`, `bypass_blacklist=90`, `access_backend=80`), all resolved from the actual caller (`agent.caller.permission_level`, snapshotted once per cycle like `modules/sysops` does) rather than a static config value — fixes a bug where `backend_access=True`'s guard read `agent.config.permissions.level`, an attribute `PermissionsConfig` never defines (it only has `minimal_tokens: bool`). The key names still say whitelist/blacklist for config compatibility; they now select which policy file applies. `bypass_blacklist` skips policy checks entirely. The tool is registered with `min_permission=use_whitelist`, so allow-list-tier callers can see/call it at all — enforcement of what they can actually run happens inside `_dispatch()`.
- Tests: `tests/test_shell_policy.py` is a must-deny/must-allow corpus run against the **real shipped YAML**, including a check that every shipped rule has a test case proving it fires. `tests/test_shell.py` covers tier routing and fail-closed behaviour against throwaway fixture policies.

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

## Instance Layout (`utils/instance.py`)

An *instance* is a self-contained directory holding one agent's config, workspace, and internal data. Resolved by every CLI command the same way: `--dir` flag → nearest ancestor of CWD literally named `.tinyctx` → `.tinyctx/` child of CWD → `~/.tinyctx`. This is what makes multiple concurrent agents possible — each just needs its own instance directory.

```
<instance>/                 e.g. ~/.tinyctx, or anywhere via --dir
├── config.yaml             Loaded by default from here (workspace.path / data.path default relative to this file)
├── .env                     Optional. KEY=VALUE per line (e.g. DISCORD_BOT_TOKEN=...). Loaded via
│                            `utils/instance.py`'s `load_instance_env()` — with override=True, so
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

Docker Compose (`compose.yaml`, always at the repo root, shared across instances) is invoked with `-f <repo>/compose.yaml -p <project>` plus env vars (`TINYCTX_CONFIG_FILE`, `TINYCTX_WORKSPACE`, `TINYCTX_DATA`, `TINYCTX_PORT`, `TINYCTX_INSTANCE`, `TINYCTX_TAG`) computed by `utils/instance.py` from the resolved instance dir — see `compose_env()`. `TINYCTX_TAG` is a separate, short (6 hex char) hash from `TINYCTX_INSTANCE` because Docker bridge interface names are capped at 15 chars (`IFNAMSIZ`) on Linux.

Non-Docker launches (`onboard`'s direct `python main.py` spawn) instead set `TINYCTX_CONFIG_FILE` in the subprocess env; `main.py` reads it if present, else defaults to `config.yaml` relative to CWD.

---

## Dependency Notes

Key packages: `aiohttp`, `rich`, `questionary`, `mcp`, `tiktoken`, `structlog`, `tenacity`, `ddgs`, `playwright`, `pdfplumber`, `python-docx`, `croniter`, `python-dotenv`, `discord.py`, `jinja2`, `numpy`.

Python ≥ 3.14 required.

Install: `pip install -e .` then `python -m TinyCTX onboard`.

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

Modules live under `TinyCTX/modules/<name>/`. Auto-discovered if they have `__main__.py` or `__init__.py`.

Each module may expose:
- `register_runtime(runtime)` — called once at startup
- `register_agent(cycle)` — called per `AgentCycle`

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

### `system_prompt`
Injects SOUL.md, AGENTS.md, TOOLS.md into the system prompt via `register_prompt` providers.

### `rag`
Indexes named databank folders under `workspace/rag/` — BM25 or embedding search via `rag_search`/`set_auto_rag_databanks` tools.
- `lorefile.py` — parses `*.md` YAML frontmatter (`name`, `mode`, `keys`, `secondary_keys`, `constant`, `selective`, `selective_logic`, `case_sensitive`, `whole_words`, `disabled`) for keyword-triggered lore entries; `convert_lorebook_json` migrates legacy SillyTavern JSON lorebooks
- `databanks.py` — `FilesDataBank` (only databank kind), `_entry_cache` keyed by `(path, mtime)`
- `__main__.py` — pre-assemble hook calling `auto_inject`; module state is one `_RagState` dataclass (`_state`)
- Config: `default_auto_targets` in `EXTENSION_META["default_config"]`

### `memory` (v2)
Scoped LadybugDB property-graph knowledge store at `<instance>/data/memory/memory.lbug`. Design doc: `modules/memory/PLAN.md`.
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

### `cron`
CRON.json-backed job scheduler; creates agent turns at specified times.

### `filesystem`
`view`, `write_file`, `edit_file`, `grep`, `glob_search` tools. Write tools sandboxed to `workspace/`; read tools can also reach `filesystem.read_only_paths` from config.yaml. `view()` returns images via `IMAGE_BLOCK_PREFIX`, unwrapped by `agent._execute_tool`.

### `shell`
`shell` tool, runs in workspace directory, Linux only.
- `validate.py` — AST-based command validation via `tree-sitter-bash`
- `policy.py` — compiles rules from YAML
- Two policy files: `deny.yaml` (default_action: allow, for callers ≥ neutral) and `allow.yaml` (default_action: deny, for callers below neutral), both overridable via `extra.shell.policy.{deny,allow}`; compose via `extends:` (`builtin:allow`, `builtin:deny`, or a relative path)
- `extra.shell.min_permission` (default 30), `extra.shell.policies` (tiered policy list), `extra.shell.permissions.access_backend` (default 80, sandbox vs main container)
- Design doc: `modules/shell/PLAN.md`; example: `modules/shell/example.instance-allow.yaml`
- Tests: `tests/test_shell_policy.py` (shipped-YAML corpus), `tests/test_shell.py` (tier routing, fail-closed)

### `web`
`web_search` (DuckDuckGo via `ddgs`) and `open_url` (Camoufox — anti-detect Firefox), plus `click`/`type_text`/`extract_text`/`extract_html`/`screenshot_browser`/`wait_for` acting on the last-loaded page.
- `config.web.headless`: `true` | `false` | `"virtual"` (default, Xvfb)
- Interstitial handling: `_settle_navigation()`, `_CHALLENGE_SELECTORS`, `_wait_for_dom_stable()`; budget `config.web.settle_timeout_ms`
- Screenshots → `workspace/outputs/browser/` (`config.web.output_dir`), inlined via `IMAGE_BLOCK_PREFIX` unless over `config.web.screenshot_max_bytes`

### `comfyui`
`generate_image_comfyui(workflow, positive_prompt, negative_prompt, dimensions="1024x1024", seed=0)` tool.
- Workflow JSON files live in `<instance>/config/comfyui/<name>.json`, resolved via `utils/instance.py::runtime_config_dir()`
- `filter.py` — NudeNet-based safety filter (hard/soft blocked labels, censor-in-place)
- Marker substitution: `MARKER>>name<<MARKER` for `positive-prompt`, `negative-prompt`, `seed`, `width`, `height`
- `Permission.IMAGE_GEN` gating; config under `comfyui:` (host/port/api_key/timeout/unload_after/safety_filter)
- Outputs: `workspace/outputs/comfyui/`

### `ctx_tools`
Context-assembly hooks only — registers **no tools**. Wired via `register_agent(cycle)`. Config in `EXTENSION_META["default_config"]`.
- **dedup** — suppresses repeated identical tool call+result (`same_call_dedup_after`, default 2)
- **cot_strip** — strips `<think>` blocks per `trim_thinking` (`"all" | "auto" | "none"`, default `"auto"`)
- **trim** — replaces/truncates old tool-result turns (`tool_output.trim_after`/`truncate_after`/`max_chars`)
- **tokenade** — blocks turns over `tokenade_threshold` (default 20000) tokens
- **`token_sanitize`** — referenced by `tests/test_ctx_tools.py` but not implemented in `__main__.py`; tests fail, known incomplete

### `equipment_manifest`
Renders `EM.md` (Jinja2) as a system prompt every turn.
- `equipment_manifest` (role=system, static vars) — cache-stable
- `equipment_manifest_footer` (role=user, volatile vars: `time`, `time_since_last_message`) — from `EM_FOOTER.md` or a built-in default
- `trusted` resolved via `UserStore.get_user(author_id)` (username lookup, not `get_by_platform`)
- `em_path` config key resolution: `""` → `EM.md` next to module; `"workspace:X"` → under workspace root

### `concurrency`
Concurrent Forks. Design doc: `docs/PLAN.md`. Registers `running_forks` roster prompt provider (role=user) plus:
- `spawn_fork(prompt)` → `run_id` — starts a run on a fresh branch off caller's head
- `nudge_fork(run_id, message)` — advisory one-way message to a peer

Lifecycle lives in `runtime.py`:
- `Run` — in-memory handle (`id`, `session_key`, `intent`, `root_node_id`, `status`, `inbox`)
- `_settled: session_key → node_id`
- `finish_run()`, one `asyncio.Lock` per session, `Exogenous(kind, role, content)` inbox entries
- Scoped by `Run.session_key` (bridge cursor key), not `SessionEnvironment`
- Capacity capped by `Runtime._semaphore` (`max_workers`, default 8)
- Tests: `tests/test_concurrency.py`

### `skills`
`use_skill(name)` tool. Loads `SKILL.md` from `workspace/skills/<name>/` (agentskills.io convention). Frontmatter `tools:` list enables deferred tools on load.

### `todo`
`todo_read` / `todo_write`. Session-scoped task checklist.

### `present`
`present(paths)` tool. Emits `AgentOutboundFiles` events.

### `mcp`
MCP server integration; loads configured MCP servers and registers their tools into the cycle.

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

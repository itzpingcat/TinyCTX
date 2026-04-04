# CLAUDE.md

## What this is

An ultra-lightweight agentic assistant framework. A single Python process runs one or more **bridges** (CLI, Discord, Matrix) that feed messages into a **router**, which routes them to per-session **agent loops**. An optional HTTP/SSE **gateway** exposes the router to external clients (SillyTavern, custom integrations, etc.). Each loop runs a 6-stage cycle: intake → async pre-assemble hooks → context assembly → LLM inference → tool execution → result backfill → reply streaming.

Everything is async (asyncio + aiohttp). No frameworks. No ORM. No magic.

---

## Running it

```bash
cp example.config.yaml config.yaml
# Edit config.yaml — set models, llm.primary, api_key_env
export ANTHROPIC_API_KEY=sk-...
pip install -r requirements.txt
python main.py
```

In the CLI bridge: type `/reset` to clear context, `exit` to quit.

Run tests: `python -m pytest -v`

Deps: `pip install -r requirements.txt` (includes `structlog`, `tenacity`)

Note: `matrix-nio[e2e]` requires cmake + libolm to compile. It is commented out in
`requirements.txt` by default. Only uncomment if you have a C build environment and need E2EE.

---

## Architecture

```
main.py
  ├── gateway/__main__.py  (if gateway.enabled) — HTTP/SSE API, external clients connect here
  └── loads bridges/*      (any dir with __main__.py + config.bridges.<n>.enabled=true)
      └── each bridge calls router.push(InboundMessage)
          └── Router → _SessionRouter → Lane → AgentLoop.run()
              ├── await ctx.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
              └── ctx.assemble() → LLM inference → tool execution
                  └── AgentLoop loads modules/* on init (any dir with register(agent))
```

**Key files:**

| File | Role |
|------|------|
| `contracts.py` | Pure data types. No logic. Everything imports from here, never the reverse. `InboundMessage` carries `author` (`UserIdentity`) for all messages. |
| `config/` | YAML loader + env var resolution. `WorkspaceConfig` (global workspace path). `ModelConfig` supports `kind: chat` (default) or `kind: embedding`, plus `vision: bool`. `AttachmentConfig` (inline thresholds). |
| `router.py` | Session routing. One `Lane` (bounded queue maxsize=32 + crash-recovering worker task) per `node_id` (cursor). `router.push()` returns `bool` — `False` means lane queue full. Bridges register platform/session handlers. `router.open_lane(node_id, platform)` eagerly creates a lane and registers its platform without enqueuing any work — used by CLI at startup and after `/reset`. |
| `gateway/` | HTTP/SSE API gateway. External clients (SillyTavern, custom scripts, etc.) connect here. `run(router, cfg)` called by `main.py`. Accepts `attachments: [{name, data_b64, mime_type}]` on POST /message. |
| `agent.py` | The 6-stage loop. Owns `Context`, `ToolCallHandler`, and LLM pool. Yields `AgentEvent` stream. Skips embedding models when building LLM pool. Stage 1 calls `build_content_blocks` when `msg.attachments` is non-empty. |
| `context.py` | Dialogue history + hook pipeline. Sync stages: `pre_assemble`, `filter_turn`, `transform_turn`, `post_assemble`. Async stage: `HOOK_PRE_ASSEMBLE_ASYNC` (awaited by agent before each `assemble()` call). Smart `delete()` / `edit()` / `strip_tool_calls()` for dialogue mutation. `HistoryEntry.content` is `str \| list` — list for user turns with attachments. |
| `ai.py` | Async OpenAI-compatible clients. `LLM` streams SSE → `TextDelta` / `ToolCallAssembled` / `LLMError`. Retries on `ClientConnectionError` (3 attempts, exp backoff via tenacity). `Embedder` calls `/v1/embeddings`, auto-batches, numpy fast-path. |
| `utils/tool_handler.py` | Registers Python functions (sync or async) as LLM tools. Auto-extracts JSON schema from type hints + docstring `Args:` block. Maintains an `enabled` set — only enabled tools are sent to the LLM. Deferred tools live in `self.tools` but not `self.enabled`; the agent discovers them via `tools_search`. |
| `utils/bm25.py` | Pure-stdlib in-memory Okapi BM25. Used by `tools_search` to rank tool names+descriptions against a query. Tokeniser splits on underscores/hyphens so `web_search` matches query `"search"`. |
| `utils/attachments.py` | Attachment classification, saving, and LLM content-block assembly. Pure utility — no tools, hooks, or prompts. Called by bridges and the gateway. |
| `utils/commands.py` | `CommandRegistry` — slash-command registry shared across all bridges. Modules register commands at `load_modules()` time via `agent.commands`. Bridges call `router.commands.dispatch(text, context)` before pushing to the router. Re-registration of the same `(namespace, sub)` replaces the old entry (last-write-wins, no duplicates). |

**Session identity:**

Sessions are represented as tree branches in `agent.db` (SQLite). Each bridge holds a **cursor** — a `node_id` string pointing at the tail of its branch. `InboundMessage.tail_node_id` carries this cursor into the router. There are no `SessionKey`, `ChatType`, or `sessions/*.json` files — both are fully removed from `contracts.py`.

**Per-bridge cursor storage:**
- CLI: `workspace/cursors/cli`
- Discord: `workspace/cursors/discord.json` (by channel_id)
- Matrix: `workspace/cursors/matrix.json` (by room_id)
- Gateway: `workspace/cursors/gateway.json` (by session_id → node_id)
- Cron: `cursor_node_id` field per job in `CRON.json`
- Heartbeat: `_heartbeat_cursor_node_id` attribute on the agent instance; node created once in `agent.db` as a child of root (or the session tail, if `branch_from: "session"`), then reused for all subsequent ticks

**Group chat sender attribution:**

`HistoryEntry` carries `author_id: str | None` — set to the sender's `user_id` for group chat user turns, `None` for DM / assistant / tool / system turns. Bridges populate this via `HistoryEntry.user(content, author_id=...)`. Persisted as `author_id` on the DB node and exposed in the gateway `GET /history` response. Bridges own all group-specific logic (buffering, mention detection, `/reset` admin checks) and push pre-formatted attributed text to the router.

**Agent event stream:**

`AgentLoop.run()` yields `AgentEvent` objects. Bridges and the gateway consume these:

| Event | When |
|-------|------|
| `AgentTextChunk` | Streaming text token (is_partial) |
| `AgentTextFinal` | Final text or stream-close sentinel |
| `AgentToolCall` | Tool dispatched (before execution) |
| `AgentToolResult` | Tool result (after execution) |
| `AgentError` | LLM error or cycle limit |

**Event handler routing (Router):**

- `register_platform_handler(platform, fn)` — fallback for all sessions on a platform (used by CLI, cron, discord, matrix)
- `register_cursor_handler(node_id, fn)` — per-cursor, takes priority; used by gateway for SSE streams
- `unregister_cursor_handler(node_id)` — call in `finally` when SSE stream ends
- `open_lane(node_id, platform)` — eagerly open a lane without enqueuing work; registers platform atomically

---

## Attachments

Attachments flow from bridges → `InboundMessage.attachments` → `agent.py` Stage 1 → `utils/attachments.py` → `HistoryEntry.content` (list of content blocks).

**`contracts.py` types:**

- `Attachment(filename, data: bytes, mime_type, kind: AttachmentKind)` — frozen dataclass
- `AttachmentKind`: `IMAGE`, `TEXT`, `DOCUMENT`, `BINARY`
- `ContentType`: `TEXT`, `MIXED` (text + attachments), `ATTACHMENT_ONLY` (no text)
- `content_type_for(text, has_attachments)` — helper used by all bridges and gateway

**`utils/attachments.py`:**

- `classify(att)` — sniffs `AttachmentKind` from mime_type + extension. Extension takes priority for text types (bridges lie about MIME).
- `save_upload(att, uploads_dir)` — writes bytes to `workspace/uploads/`, collision-avoids with `_1`, `_2` suffixes. Always runs, even for reference-only files.
- `build_content_blocks(text, attachments, model_cfg, att_cfg, workspace)` — returns `list[dict]` (OpenAI-compat content blocks) or plain `str` if all files are reference-only.

**Content block strategies:**

| Kind | Vision model | Non-vision / non-vision model |
|------|-------------|-------------------------------|
| `IMAGE` | `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}` | Reference note only |
| `TEXT` | Fenced code block in `{"type": "text", ...}` | Same |
| `DOCUMENT` (.pdf) | Text extracted via `pdfplumber` if installed, else reference note | Same |
| `DOCUMENT` (.docx) | Text extracted via `python-docx` if installed, else reference note | Same |
| `BINARY` | Reference note only | Reference note only |

Both `pdfplumber` and `python-docx` are soft dependencies — the bridge degrades gracefully to a reference note if they are absent.

**Inline thresholds** (`AttachmentConfig`, configurable via `attachments:` in config.yaml):

```yaml
attachments:
  inline_max_files: 3       # max files to inline per message (default 3)
  inline_max_bytes: 204800  # max total raw bytes to inline (default ~200 KB)
  uploads_dir: uploads      # relative to workspace root
```

Once either threshold is exceeded, remaining files become reference-only regardless of kind.

**Vision flag** on `ModelConfig`:

```yaml
models:
  smart:
    model: claude-sonnet-4-20250514
    vision: true   # enables image_url blocks for this model
```

Default is `false`. Images sent to a non-vision model become a reference note instead.

**Bridge attachment handling:**

- **Discord** — downloads each `message.attachment` via aiohttp from Discord's CDN. `content_type` from `a.content_type`, filename from `a.filename`.
- **Matrix** — registers `_on_media` callbacks for `RoomMessageImage`, `RoomMessageFile`, `RoomMessageAudio`, `RoomMessageVideo` (requires matrix-nio ≥ 0.20; degrades gracefully with a warning on older versions). Media events are buffered in `_pending_attachments[sender:room_id]` and attached to the next text message from that sender in that room.
- **Gateway** — accepts `attachments: [{name, data_b64, mime_type}]` in POST /message JSON body. Decodes base64 server-side.

**`HistoryEntry.content` as list:**

When a user turn has inlineable attachments, `content` is a `list[dict]` of OpenAI-compat content blocks rather than a plain `str`. This is transparent to most of the pipeline:
- `_render()` in `context.py` passes it through unchanged.
- The merge guard in `assemble()` never merges two adjacent turns when either has list content (images must stay distinct).
- `_count_tokens()` handles both str and list content.
- `_flush_history` / `_restore_history` in `agent.py` JSON round-trips lists correctly.

---

## Config structure

```yaml
context: 16384

models:
  smart:                        # kind: chat (default)
    base_url: https://api.anthropic.com/v1
    model: claude-sonnet-4-20250514
    api_key_env: ANTHROPIC_API_KEY
    max_tokens: 4096
    temperature: 0.7
    vision: true                # set true for multimodal models
  embed:                        # kind: embedding — excluded from llm: routing
    kind: embedding
    base_url: http://localhost:11434/v1
    api_key_env: N/A
    model: nomic-embed-text

llm:
  primary: smart
  fallback: [fast, local]
  fallback_on:
    any_error: false
    http_codes: [429, 500, 502, 503, 504]

gateway:                        # HTTP/SSE API gateway (external clients)
  enabled: true
  host: 127.0.0.1
  port: 8080
  api_key: "your-secret-token"

workspace:
  path: ~/.tinyctx              # global — all modules resolve paths here

attachments:                    # optional — shown with defaults
  inline_max_files: 3
  inline_max_bytes: 204800
  uploads_dir: uploads

# router: (internal TCP config, rarely needed)
#   host: 127.0.0.1
#   port: 8765

# Module config lives under extra top-level keys (e.g. memory_search:, mcp:)
# agent.config.extra.get("memory_search", {}) etc.
```

Modules access workspace via `agent.config.workspace.path`. There is no `memory.workspace_path` anymore — it was renamed.

---

## Gateway API

All endpoints require `Authorization: Bearer <api_key>`. Health is always public.

```
GET    /v1/sessions                              list all sessions (active lanes + cursor map)
DELETE /v1/sessions/{id}                         reset in-memory context (tree preserved)
POST   /v1/sessions/{id}/message                 send message; SSE stream or JSON response
PUT    /v1/sessions/{id}/generation              trigger generation with no new user message
DELETE /v1/sessions/{id}/generation             abort in-flight generation (204, no-op if idle)
POST   /v1/sessions/{id}/reset                  alias for DELETE session (backwards compat)
GET    /v1/sessions/{id}/history                raw dialogue from agent.db ancestor chain
GET    /v1/workspace/files/{path}               read any file under workspace root
PUT    /v1/workspace/files/{path}               write any file under workspace root
GET    /v1/health                               status, uptime_s, per-lane queue/turn info
```

**Session identity:** `{id}` is a human-readable string (e.g. `"main"`, `"user-123"`). The gateway maps it to a `node_id` UUID in `workspace/cursors/gateway.json`. On first use a child of the global DB root is created and persisted; subsequent calls reuse the same node_id.

**Backpressure:** `/message` and `/generation` return HTTP 429 if the lane queue is full.

**POST /message body:**
```json
{
  "text": "what is in this image?",
  "stream": true,
  "attachments": [
    {"name": "photo.png", "data_b64": "<base64>", "mime_type": "image/png"}
  ]
}
```
`text` or `attachments` (or both) must be present. `stream` defaults to `true`.

**PUT /generation body:** `{ "stream": true }` (optional). Queues a synthetic turn — no user message is added; agent generates against current context as-is.

**SSE event types** (streaming responses):
```json
{"type": "text_chunk",  "text": "..."}
{"type": "tool_call",   "call_id": "...", "name": "...", "args": {...}}
{"type": "tool_result", "call_id": "...", "name": "...", "output": "...", "is_error": false}
{"type": "text_final",  "text": "..."}
{"type": "error",       "message": "..."}
{"type": "done"}
```
Non-streaming responses return `{ "text": "..." }` (120s timeout).

**GET /sessions response:**
```json
[{ "id": "main", "node_id": "<uuid>", "turns": 5, "queue_depth": 0, "queue_max": 32, "is_active": true }]
```
Includes all sessions in the cursor map (active or not) plus any active lanes from other bridges (shown with `node_id` as `id`).

**GET /history response:** Ancestor chain from `agent.db` (root → current tail), system session-marker nodes filtered out. Reads directly from DB — no active lane required:
```json
[{ "id": "<uuid>", "role": "user", "content": "...", "tool_calls": null, "tool_call_id": null, "author_id": null, "created_at": 1234567890.0 }]
```

**GET /health response:**
```json
{ "status": "ok", "uptime_s": 42.1, "lanes": { "<node_id>": { "session_id": "main", "turns": 5, "queue_depth": 0, "queue_max": 32 } } }
```

**Workspace file paths** are resolved relative to `workspace.path`. Path traversal (`..`) is rejected with 403.

---

## Module loading and command registration

`AgentLoop.__init__` does **not** call `load_modules()` for normal (non-subagent) loops. Instead, `Lane.__post_init__` wires `self.loop.commands = self.router.commands` first, then calls `self.loop.load_modules()`. This ensures modules that call `agent.commands.register(...)` see the shared router registry, not the throwaway `CommandRegistry` created in `__init__`.

Subagent loops (`is_subagent=True`, used for background branches) have no `Lane` and call `load_modules()` immediately in `__init__` — they don't need the shared command registry.

The CLI bridge calls `router.open_lane(cursor, platform)` at startup and after every `/reset` to trigger module loading before the user types anything, so `/help` shows all commands immediately.

---

## Background branches

A background branch is a child node written into `agent.db` off the current tail, with a fresh `AgentLoop` (`is_subagent=True`) running a synthetic turn on it. Events are discarded. The caller's cursor never moves.

**How to fire one:**

```python
# From anywhere — a hook, a command handler, a module:
from agent import _run_background
import asyncio

branch_node = agent._db.add_node(parent_id=agent.context.tail_node_id, role="user", content="...")
asyncio.create_task(_run_background(branch_node.id, agent.config))
```

`asyncio.create_task` schedules the branch on the event loop without blocking the caller. The branch runs independently and writes its own nodes into `agent.db`.

**`register_background_hook(fn)`** registers a hook called after every interactive turn with `(tail_node_id, config)`. The hook is responsible for calling `create_task` itself if it needs background work:

```python
async def _my_hook(tail_node_id: str, config) -> None:
    opening = agent._db.add_node(parent_id=tail_node_id, role="user", content="...")
    asyncio.create_task(_run_background(opening.id, config))

agent.register_background_hook(_my_hook)
```

Background hooks are skipped for synthetic turns (they are themselves background work).

**`queue_background_branch()` is removed.** It was a deferred-flush mechanism that only worked inside `AgentLoop.run()`. All callers now use `asyncio.create_task(_run_background(...))` directly.

---

## Adding a bridge

1. Create `bridges/<n>/__main__.py` with an async `run(router)` function.
2. Register a platform handler: `router.register_platform_handler("<n>", handler)`.
3. Push messages: `await router.push(InboundMessage(...))`.
4. Use `content_type_for(text, bool(attachments))` from `contracts.py` to set `content_type`.
5. Handle `AgentEvent` objects in the handler — switch on type.
6. Add entry to `config.yaml` under `bridges:` with `enabled: true`.
   For the CLI bridge, `options.quiet_startup: true` and `options.log_level: WARNING`
   keep the terminal UI clean; switch `log_level` to `inherit` only when you
   explicitly want module/runtime INFO logs in the chat session.
7. Tokens from env vars only — do not add bridge-specific dataclasses to `config/`.

**Slash command dispatch in bridges:**
Before pushing a message to the router, check if it's a slash command:
```python
if text.startswith("/"):
    ctx = {"router": router, "cursor": node_id, ...}  # platform-specific keys
    handled = await router.commands.dispatch(text, ctx)
    if handled:
        return
```
The context dict must always include `"router"` and `"cursor"`. Additional keys (`"channel"`, `"message"`, `"send"`, etc.) are platform-specific and optional. See existing bridges for examples.

---

## Adding a module

1. Create `modules/<n>/__init__.py` (holds `EXTENSION_META` with `default_config`) and `modules/<n>/__main__.py`.
2. Expose `register(agent)` in `__main__.py`.
3. Wire tools: `agent.tool_handler.register_tool(fn, always_on=False)` — sync or async functions both work. Pass `always_on=True` for tools that should always appear in the LLM's tool list. Omit it (or pass `False`) for deferred tools that the agent discovers via `tools_search`.
4. Register sync prompt providers: `agent.context.register_prompt(pid, fn)`.
5. Register sync hooks: `agent.context.register_hook(stage, fn)`.
6. Register async pre-assemble hooks: `agent.context.register_hook(HOOK_PRE_ASSEMBLE_ASYNC, async_fn)`.

Modules must not import from `router.py`, `gateway/`, or any bridge.

**Registering slash commands:**
```python
def register(agent) -> None:
    registry = getattr(agent, "commands", None)
    if registry is not None:
        async def _cmd_handler(args: list[str], context: dict) -> None:
            ...
        registry.register("namespace", "subcommand", _cmd_handler, help="Description")
```
`agent.commands` is the shared `CommandRegistry` — always available at `register()` time because `Lane` wires it before calling `load_modules()`. Re-registering the same `(namespace, sub)` silently replaces the old handler.

**Async hooks** (`HOOK_PRE_ASSEMBLE_ASYNC`) run before every `assemble()` call inside `agent.run()`. Use them for I/O that must complete before the context is built (e.g. embedding a query, syncing a search index). Import the constant: `from context import HOOK_PRE_ASSEMBLE_ASYNC`.

---

## Key conventions

- **`contracts.py` is the shared language.** Never import router/agent/bridges from contracts. Never import contracts from ai.py. `SessionKey` and `ChatType` no longer exist — do not add them back.
- **Modules are self-contained.** No hardcoded module names anywhere in core.
- **Bridges are self-contained.** No hardcoded bridge names anywhere in core.
- **Config is generic.** `BridgeConfig(enabled, options)` for all bridges. Tokens from env vars.
- **Workspace is `agent.config.workspace.path`** — a resolved absolute `Path`. All modules use this. Default `~/.tinyctx`.
- **Embedding models** use `kind: embedding` in `models:`. Access via `agent.config.get_embedding_model("name")`. Build with `Embedder.from_config(cfg)` from `ai.py`.
- **Vision models** use `vision: true` in `models:`. Non-vision models receive a reference note instead of image blocks.
- **Tool functions can be sync or async.** `ToolCallHandler.execute_tool_call` handles both.
- **Context hooks run in priority order** (lower = first). `priority=0` for early hooks (dedup, memory), `priority=10+` for later (trim).
- **Token budget telemetry:** agent logs at INFO when context hits 80% of `context:` limit, WARNING at 95%. These are signals to implement/trigger the compaction module.
- **LLM retries:** `ai.LLM` retries `ClientConnectionError` up to 3× with exponential backoff (1–8s). Other errors (`LLMError`) pass through immediately.
- **Queue backpressure:** Lane queues are bounded (32 by default, change `LANE_QUEUE_MAX` in `router.py`). Full queues return `False` from `router.push()` — gateway surfaces this as 429.
- **Graceful shutdown:** SIGTERM/SIGINT drain in-flight turns before exit.
- **Structured logging:** `structlog` with timestamped console output. All loggers use `logging.getLogger(__name__)` — no changes needed in modules.
- **Always prefer dynamic discovery over hardcoding** — scan filesystem, infer from config, auto-register at runtime.
- **Suggest before implementing.** For non-trivial changes, propose 2–3 approaches with tradeoffs before writing code.

---

## Tool system

`ToolCallHandler` (`utils/tool_handler.py`) has two dicts:

- `self.tools` — all registered tools. Populated at module `register()` time.
- `self.enabled` — the subset currently sent to the LLM on every call.

**Registration:**

```python
agent.tool_handler.register_tool(fn, always_on=True)   # always in tool list
agent.tool_handler.register_tool(fn)                   # deferred — not in tool list until searched
agent.tool_handler.enable("tool_name")                 # enable programmatically (returns bool)
```

**`tools_search` (always on):** Registered by `AgentLoop.__init__` before any module loads. The agent calls it to discover and enable deferred tools by keyword. Backed by in-memory BM25 (`utils/bm25.py`) — only tools with a positive BM25 score are enabled. Tool names with underscores/hyphens are split into words before indexing, so `"search"` matches `web_search`.

**Per-module defaults:**

| Module | Always-on tools | Deferred tools |
|--------|----------------|----------------|
| `filesystem` | `shell`, `view`, `write_file`, `str_replace`, `grep`, `glob_search` | — |
| `memory` | `memory_search` | — |
| `skills` | `use_skill` | — |
| `web` | `web_search`, `browse_url`, `navigate` | `http_request`, `click`, `type_text`, `extract_text`, `extract_html`, `screenshot`, `wait_for`, `manage_browser` |
| `todo` | — | `todo_write`, `todo_read` |
| `mcp` | configurable per-tool (see below) | all tools default to deferred |
| `cron` | `cron_list` | — |

All defaults can be overridden in `config.yaml` per-module under a `tools:` sub-key:

```yaml
web:
  tools:
    http_request: always_on   # promote from deferred
    screenshot:   disabled    # never register

memory_search:
  tools:
    memory_search: deferred   # demote from always_on

skills:
  tools:
    use_skill: deferred

mcp:
  servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
      tools:
        read_file:   always_on
        write_file:  deferred
        delete_file: disabled
```

**Visibility values:** `always_on` — in every LLM call. `deferred` — registered but hidden until `tools_search` enables it. `disabled` — never registered.

**`get_tool_definitions()`** only returns tools in `self.enabled`. `execute_tool_call()` dispatches via `self.tools` (not `self.enabled`), so a tool enabled mid-turn is callable in the same cycle.

**`reset()` does not clear `enabled`.** The enabled set survives a `/reset` — tools the agent searched for in a prior turn remain enabled for the next session.

**Memory hook is cycle-aware.** `HOOK_PRE_ASSEMBLE_ASYNC` checks the last dialogue entry role before doing any work — if it's `tool` or `assistant` (mid-tool-loop), it returns immediately and reuses the cached `ctx.state["memory_search_results"]` from cycle 0. This avoids redundant embedding round-trips on every tool-call cycle within a turn.

---

## Context mutation

`Context` supports direct dialogue mutation (all methods re-index after changes):

| Method | Behaviour |
|--------|-----------|
| `edit(entry_id, content)` | Replace content in-place. No cascade. |
| `delete(entry_id)` | Smart-delete: removes entry + dependents (assistant tool_calls cascade to their results; tool results cascade back to their assistant turn + siblings). |
| `strip_tool_calls(entry_id)` | Remove `tool_calls` from an assistant entry and drop its results, preserving the assistant's text content. |
| `clear()` | Wipe entire dialogue. |

`HistoryEntry` fields: `role`, `content` (str or list), `id`, `index`, `tool_calls`, `tool_call_id`, `author_id` (str or None).

Token-budget trimming in `assemble()` follows the same rules: assistant turns with text content preserve the text when tool calls are trimmed. Adjacent user/assistant turns are only merged when both have plain-string content — list content (attachment blocks) is never merged.

---

## Modules reference

| Module | What it does |
|--------|-------------|
| `memory` | Static: injects SOUL.md, AGENTS.md, MEMORY.md as system prompts. Dynamic: hybrid BM25+vector search over `workspace/memory/**/*.md` via async pre-assemble hook + `memory_search` tool. Context nudge: when token delta exceeds threshold, spawns a background branch for memory consolidation instead of injecting inline. Config key: `memory_search:`. |
| `filesystem` | Tools: `shell`, `view`, `write_file`, `str_replace`, `grep`, `glob_search` — all always-on. `grep` wraps ripgrep with automatic Python fallback; supports files/content/count output modes, glob filtering, file type filtering, context lines, and result limits. `glob_search` finds files by pattern (pathlib), sorted by mtime. **Read-before-write guard:** `write_file` and `str_replace` require the target file to have been read via `view()` first; they also reject writes if the file has been modified since the last read (staleness detection). New file creation is exempt. Sandboxed to workspace. Shell execution split into `shell.py` (platform detection, blacklist, subprocess dispatch). On Linux/macOS runs via `bash -c`; on Windows via `powershell -NonInteractive`. Blacklist (`blacklist.txt`, glob patterns, case-insensitive) enforced on both platforms — covers bulk destruction, RCE, privilege escalation, persistence, system path writes, and more. Blocked commands return an error string. Blacklist loaded at `register()` time; restart to reload. `str_replace` supports `replace_all=true` for multi-occurrence replacements. |
| `web` | Tools: `web_search` (DuckDuckGo), `browse_url` (direct page fetch/scrape with optional `prompt` for LLM-powered content extraction), and `navigate` are always-on. `browse_url(prompt=...)` fetches a page then runs a secondary LLM call to extract/summarize specific information — saves context by not returning the full page. `http_request`, `click`, `type_text`, `extract_text`, `extract_html`, `screenshot`, `wait_for`, `manage_browser` (Playwright) are deferred. |
| `todo` | Persistent per-session task checklist. Deferred tools: `todo_write` (create/update task list), `todo_read` (view current tasks). State persisted to `workspace/TODO.json`. Current task list injected into system prompt every turn so the agent always sees its progress. Statuses: `pending`, `in_progress`, `completed`. Use for multi-step work with 3+ steps. |
| `cron` | Scheduled agent turns. Jobs in `workspace/CRON.json`. Schedule kinds: `every`, `at`, `cron` (requires `croniter`). Tool: `cron_list`. Each job holds its own `cursor_node_id` (child of global root, created on first run). `reset_after_run: true` rewinds the cursor to the job's root node rather than wiping the DB. |
| `heartbeat` | Periodic timer turns on an isolated DB branch — never pollutes the user's conversation thread. On `register()`, creates a session-init node in `agent.db` and stores its `node_id` as `agent._heartbeat_cursor_node_id`. All ticks push through the gateway (own `Lane` + `AgentLoop`); the cursor advances after each turn and is cached for the next. `branch_from: "root"` (default) — fully isolated; `branch_from: "session"` — branches off the user session tail at startup. Agent replies `HEARTBEAT_OK` if nothing needs attention; otherwise triggers a continuation loop (up to `max_continuations`). Configurable active hours. |
| `ctx_tools` | Context pipeline hooks: deduplicates repeated identical tool calls, strips `<think>` CoT blocks from old turns, truncates/trims large tool outputs. |
| `skills` | agentskills.io standard. Scans configured dirs + `~/.agents/skills/` for `SKILL.md` files, injects a compact index as system prompt, tool: `use_skill(name)` (always-on). |
| `mcp` | stdio MCP server client. Connects at startup, discovers tools, registers them as `mcp__<server>__<tool>`. All tools deferred by default; configure per-tool visibility under `mcp.servers.<n>.tools:`. Requires `pip install mcp`. |

---

## Memory module detail

The memory module has two layers:

**Static (always on):**
- `SOUL.md` — injected at priority 0 (first in system prompt)
- `AGENTS.md` — injected at priority 10
- `MEMORY.md` — injected at priority 20
- Files are re-read every turn; edit in place with no restart needed.

**Dynamic search:**
- Recursively scans `workspace/memory/**/*.md`
- SQLite cache at `workspace/memory/cache.db` (BLOBs, not JSON; float32)
- Dirty detection: content hash + embedding model name
- Chunking strategies (configurable via `chunk_strategy:`): `markdown`, `tokens`, `chars`, `delimiter`
- Search: hybrid BM25 (FTS5) + cosine vector; numpy fast-path when available
- `memory_budget_tokens` caps injected block size; first chunk always included
- `auto_inject: true` — results injected as system prompt every turn (search runs on cycle 0 only; results cached in `ctx.state` for subsequent tool-call cycles)
- `auto_inject: false` — results only via `memory_search` tool
- **Search skips embedder** when all chunks fit within `memory_budget_tokens` (`store.total_chunks_text_tokens() ≤ budget_tokens`) — pure BM25 fetch instead, no embedding round-trip
- Config key: `memory_search:` in `config.yaml` (avoids collision with `workspace:`)
- **Context nudge:** when threshold is hit and a DB is wired, creates an opening node via `db.add_node()` off the current tail and fires `asyncio.create_task(_run_background(...))`. The background `AgentLoop` handles consolidation; the live conversation is untouched. Falls back to inline injection when no DB is wired (tests/legacy).
- **`/memory consolidate` command** — registered via `agent.commands`; fires a background branch immediately off the current tail regardless of nudge threshold.

---

## What to put in workspace files

| File | Contents |
|------|----------|
| `SOUL.md` | Agent personality and standing instructions. Loaded first, every turn. |
| `AGENTS.md` | Definitions of sub-agents, personas, or role instructions. |
| `MEMORY.md` | Long-term facts the agent should always have in context. |
| `memory/*.md` | Arbitrary knowledge files — searched semantically each turn. Subdirectories supported. |
| `memory/session-{date}.md` | Session-scoped notes written by the agent on nudge (ongoing tasks, decisions, per-session context). |
| `uploads/` | Files and images delivered by users via any bridge. Saved by `utils/attachments.py`. Small/inlineable files are encoded into content blocks; larger or binary files are saved here and the agent receives a reference note. |
| `CRON.json` | Scheduled jobs (cron module). |
| `HEARTBEAT.md` | Standing instructions for heartbeat ticks (read by agent via filesystem tools). |
| `skills/` | Skill folders following agentskills.io convention, each containing `SKILL.md`. |


## IMPORTANT SHIT AND STANDARDS

NO CONTEXT COMPACTION SHIT
NO ADDING FEATURES DIRECTLY TO AGENT.PY, ONLY MODULES
YOU ARE BANNED FROM USING PROMPT_TOOLKIT
EVERY FILE MUST BE UNDER 767 LINES OR I WILL DELETE IT
DO NOT HARDCODE PROMPT FOR STUFF LIKE SUBAGENTS, JUST PUT IT IN A SKILL
# CLAUDE.md

## What this is

An ultra-lightweight agentic assistant framework. A single Python process runs one or more **bridges** (CLI, Discord, Matrix) that feed messages into a **gateway**, which routes them to per-session **agent loops**. Each loop runs a 6-stage cycle: intake → async pre-assemble hooks → context assembly → LLM inference → tool execution → result backfill → reply streaming.

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

---

## Architecture

```
main.py
  └── loads bridges/* (any dir with __main__.py + config.bridges.<n>.enabled=true)
      └── each bridge calls gateway.push(InboundMessage)
          └── Gateway → SessionRouter → Lane → AgentLoop.run()
              ├── await ctx.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)  ← new
              └── ctx.assemble() → LLM inference → tool execution
                  └── AgentLoop loads modules/* on init (any dir with register(agent))
```

**Key files:**

| File | Role |
|------|------|
| `contracts.py` | Pure data types. No logic. Everything imports from here, never the reverse. |
| `config/` | YAML loader + env var resolution. `WorkspaceConfig` (global workspace path). `ModelConfig` supports `kind: chat` (default) or `kind: embedding`. |
| `gateway.py` | Session routing. One `Lane` (queue + worker task) per `SessionKey`. |
| `agent.py` | The 6-stage loop. Owns `Context`, `ToolCallHandler`, and LLM pool. Skips embedding models when building LLM pool. |
| `context.py` | Dialogue history + hook pipeline. Sync stages: `pre_assemble`, `filter_turn`, `transform_turn`, `post_assemble`. Async stage: `HOOK_PRE_ASSEMBLE_ASYNC` (awaited by agent before each `assemble()` call). |
| `ai.py` | Async OpenAI-compatible clients. `LLM` streams SSE → `TextDelta` / `ToolCallAssembled` / `LLMError`. `Embedder` calls `/v1/embeddings`, auto-batches, numpy fast-path. |
| `utils/tool_handler.py` | Registers Python functions (sync or async) as LLM tools. Auto-extracts JSON schema from type hints + docstring `Args:` block. |

**Session identity:**

- DM sessions: `SessionKey(chat_type=DM, conversation_id=<user_id>)` — platform-agnostic.
- Group sessions: `SessionKey(chat_type=GROUP, conversation_id=<channel_id>, platform=<platform>)` — platform-specific.

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

workspace:
  path: ~/.tinyctx              # global — all modules resolve paths here

# Module config lives under extra top-level keys (e.g. memory_search:, mcp:)
# agent.config.extra.get("memory_search", {}) etc.
```

Modules access workspace via `agent.config.workspace.path`. There is no `memory.workspace_path` anymore — it was renamed.

---

## Adding a bridge

1. Create `bridges/<n>/__main__.py` with an async `run(gateway)` function.
2. Register a reply handler: `gateway.register_reply_handler("<n>", handler)`.
3. Push messages: `await gateway.push(InboundMessage(...))`.
4. Add entry to `config.yaml` under `bridges:` with `enabled: true`.
5. Tokens from env vars only — do not add bridge-specific dataclasses to `config/`.

---

## Adding a module

1. Create `modules/<n>/__init__.py` (holds `EXTENSION_META` with `default_config`) and `modules/<n>/__main__.py`.
2. Expose `register(agent)` in `__main__.py`.
3. Wire tools: `agent.tool_handler.register_tool(fn)` — sync or async functions both work.
4. Register sync prompt providers: `agent.context.register_prompt(pid, fn)`.
5. Register sync hooks: `agent.context.register_hook(stage, fn)`.
6. Register async pre-assemble hooks: `agent.context.register_hook(HOOK_PRE_ASSEMBLE_ASYNC, async_fn)`.

Modules must not import from `gateway.py` or any bridge.

**Async hooks** (`HOOK_PRE_ASSEMBLE_ASYNC`) run before every `assemble()` call inside `agent.run()`. Use them for I/O that must complete before the context is built (e.g. embedding a query, syncing a search index). Import the constant: `from context import HOOK_PRE_ASSEMBLE_ASYNC`.

---

## Key conventions

- **`contracts.py` is the shared language.** Never import gateway/agent/bridges from contracts. Never import contracts from ai.py.
- **Modules are self-contained.** No hardcoded module names anywhere in core.
- **Bridges are self-contained.** No hardcoded bridge names anywhere in core.
- **Config is generic.** `BridgeConfig(enabled, options)` for all bridges. Tokens from env vars.
- **Workspace is `agent.config.workspace.path`** — a resolved absolute `Path`. All modules use this. Default `~/.tinyctx`.
- **Embedding models** use `kind: embedding` in `models:`. Access via `agent.config.get_embedding_model("name")`. Build with `Embedder.from_config(cfg)` from `ai.py`.
- **Tool functions can be sync or async.** `ToolCallHandler.execute_tool_call` handles both.
- **Context hooks run in priority order** (lower = first). `priority=0` for early hooks (dedup, memory), `priority=10+` for later (trim).
- **Sessions persist** to `sessions/<session_key>/<N>.json` after every turn.
- **Always prefer dynamic discovery over hardcoding** — scan filesystem, infer from config, auto-register at runtime.
- **Suggest before implementing.** For non-trivial changes, propose 2–3 approaches with tradeoffs before writing code.

---

## Modules reference

| Module | What it does |
|--------|-------------|
| `memory` | Static: injects SOUL.md, AGENTS.md, MEMORY.md as system prompts. Dynamic: hybrid BM25+vector search over `workspace/memory/**/*.md` via async pre-assemble hook + `memory_search` tool. Config key: `memory_search:`. |
| `filesystem` | Tools: `shell`, `view`, `create_file`, `str_replace`. Sandboxed to workspace. |
| `web` | Tools: `web_search` (DuckDuckGo), `http_request`, `navigate`/`click`/`type_text`/`extract_text`/`extract_html`/`screenshot`/`wait_for` (Playwright), `manage_browser`. |
| `cron` | Scheduled agent turns. Jobs in `workspace/CRON.json`. Schedule kinds: `every`, `at`, `cron` (requires `croniter`). Tool: `cron_list`. |
| `heartbeat` | Periodic timer turns. Agent replies `HEARTBEAT_OK` if nothing needs attention; otherwise triggers a continuation loop. Configurable active hours. |
| `ctx_tools` | Context pipeline hooks: deduplicates repeated identical tool calls, strips `<think>` CoT blocks from old turns, truncates/trims large tool outputs. |
| `skills` | agentskills.io standard. Scans configured dirs + `~/.agents/skills/` for `SKILL.md` files, injects a compact index as system prompt, tool: `use_skill(name)`. |
| `mcp` | stdio MCP server client. Connects at startup, discovers tools, registers them as `mcp__<server>__<tool>`. Config under `mcp.servers:`. Requires `pip install mcp`. |

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
- `auto_inject: true` — results injected as system prompt every turn
- `auto_inject: false` — results only via `memory_search` tool
- Config key: `memory_search:` in `config.yaml` (avoids collision with `workspace:`)

---

## What to put in workspace files

| File | Contents |
|------|----------|
| `SOUL.md` | Agent personality and standing instructions. Loaded first, every turn. |
| `AGENTS.md` | Definitions of sub-agents, personas, or role instructions. |
| `MEMORY.md` | Long-term facts the agent should always have in context. |
| `memory/*.md` | Arbitrary knowledge files — searched semantically each turn. Subdirectories supported. |
| `CRON.json` | Scheduled jobs (cron module). |
| `HEARTBEAT.md` | Standing instructions for heartbeat ticks (read by agent via filesystem tools). |
| `skills/` | Skill folders following agentskills.io convention, each containing `SKILL.md`. |

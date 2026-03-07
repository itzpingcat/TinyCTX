# CLAUDE.md

## What this is

An ultra-lightweight agentic assistant framework. A single Python process runs one or more **bridges** (CLI, Discord, Matrix, Telegram, etc.) that feed messages into a **gateway**, which routes them to per-session **agent loops**. Each loop runs a 6-stage cycle: intake → context assembly → LLM inference → tool execution → result backfill → reply streaming.

Everything is async (asyncio + aiohttp). No frameworks. No ORM. No magic.

---

## Running it

```bash
cp example.config.yaml config.yaml
# Edit config.yaml — set llm.model, llm.base_url, llm.api_key_env
export ANTHROPIC_API_KEY=sk-...
pip install -r requirements.txt
python main.py
```

In the CLI bridge: type `/reset` to clear context, `exit` to quit.

---

## Architecture

```
main.py
  └── loads bridges/* (any dir with __main__.py + config.bridges.<name>.enabled=true)
      └── each bridge calls gateway.push(InboundMessage)
          └── Gateway → SessionRouter → Lane → AgentLoop.run()
              └── AgentLoop loads modules/* on init (any dir with register(agent))
```

**Key files:**

| File | Role |
|------|------|
| `contracts.py` | Pure data types. No logic. Everything imports from here, never the reverse. |
| `config/` | YAML loader + env var resolution. No bridge-specific dataclasses — all bridges use generic `BridgeConfig`. |
| `gateway.py` | Session routing. One `Lane` (queue + worker task) per `SessionKey`. |
| `agent.py` | The 6-stage loop. Owns `Context`, `ToolCallHandler`, and `LLM`. |
| `context.py` | Dialogue history + 4-stage hook pipeline (pre_assemble, filter_turn, transform_turn, post_assemble). |
| `ai.py` | Async OpenAI-compatible SSE client. Yields `TextDelta`, `ToolCallAssembled`, `LLMError`. |
| `utils/tool_handler.py` | Registers Python functions as LLM tools. Auto-extracts schema from type hints + docstrings. |

**Session identity:**

- DM sessions: `SessionKey(chat_type=DM, conversation_id=<user_id>)` — platform-agnostic, same human on Discord and CLI shares one session.
- Group sessions: `SessionKey(chat_type=GROUP, conversation_id=<channel_id>, platform=<platform>)` — platform-specific.

---

## Adding a bridge

1. Create `bridges/<name>/__main__.py` with a `run(gateway)` async function.
2. Register a reply handler: `gateway.register_reply_handler("<name>", handler)`.
3. Push messages: `await gateway.push(InboundMessage(...))`.
4. Add entry to `config.yaml` under `bridges:` with `enabled: true`.
5. Store tokens in env vars — read them directly in your bridge (`os.environ.get("MY_TOKEN")`). Do not add bridge-specific dataclasses to `config/`.

The bridge name must match the directory name exactly.

---

## Adding a module

1. Create `modules/<name>/__init__.py` (can hold `EXTENSION_META`) and `modules/<name>/__main__.py`.
2. Expose `register(agent)` in `__main__.py`. Agent is an `AgentLoop` instance.
3. Wire in tools via `agent.tool_handler.register_tool(fn)`.
4. Register prompt providers via `agent.context.register_prompt(pid, fn)`.
5. Register context hooks via `agent.context.register_hook(stage, fn)`.

Modules must not import from `gateway.py` or any bridge. They receive everything through `agent`.

**Tool registration:** Just pass a typed Python function. Schema is auto-extracted from type hints and the docstring `Args:` block. First line of the docstring becomes the tool description.

---

## Working with Claude

- **Always prefer dynamic discovery over hardcoding.** If something can be scanned from the filesystem, inferred from config, or auto-registered at runtime — do that. Never hardcode bridge names, module names, tool lists, or platform enums unless strictly necessary. Follow the existing pattern: `main.py` scans `bridges/`, `agent.py` scans `modules/`, `tool_handler` introspects function signatures.
- **Suggest before implementing.** When asked to build or change something non-trivial, propose 2–3 implementation approaches with tradeoffs, then wait for confirmation before writing code. Do not pick one and run with it.

---

## Key conventions

- **`contracts.py` is the shared language.** Never import gateway/agent/bridges from contracts. Never import contracts from ai.py.
- **Modules are self-contained.** No hardcoded module names anywhere in core. `agent.py` scans `modules/` at init.
- **Bridges are self-contained.** No hardcoded bridge names anywhere in core. `main.py` scans `bridges/` at startup.
- **Config is generic.** `BridgeConfig(enabled, options)` for all bridges. Tokens come from env vars, not config.
- **Tool functions can be sync or async.** `ToolCallHandler.execute_tool_call` handles both.
- **Context hooks run in priority order** (lower = first). Use `priority=0` for early hooks (dedup), `priority=10+` for later ones (trim).
- **Sessions persist to `sessions/<session_key>/<N>.json`** after every turn. Auto-incrementing version numbers.
- **Workspace is `~/.tinyctx`** (configurable). Modules write files here. SOUL.md, MEMORY.md, AGENTS.md, CRON.json all live here.

---

## Modules reference

| Module | What it does |
|--------|-------------|
| `memory` | Injects SOUL.md, AGENTS.md, MEMORY.md as system prompt on every turn. Edit files live — no restart needed. |
| `filesystem` | Tools: `shell`, `view`, `create_file`, `str_replace`. Sandboxed to workspace. |
| `web` | Tools: `web_search` (DuckDuckGo), `http_request`, `navigate`/`click`/`type_text` (Playwright), `screenshot`. |
| `cron` | Scheduled agent turns. Jobs defined in workspace/CRON.json. Kinds: `every`, `at`, `cron`. |
| `heartbeat` | Periodic agent turns on a timer. Agent replies `HEARTBEAT_OK` if nothing needs attention. |
| `ctx_tools` | Context optimizations: deduplicates repeated tool calls, trims old tool outputs. |

---

## What to put in workspace/SOUL.md

The agent's personality and standing instructions. Loaded as the first system prompt on every turn. Example:

```markdown
You are a focused assistant. Be concise. When using tools, prefer fewer calls.
Always update MEMORY.md after learning something new about the user.
```
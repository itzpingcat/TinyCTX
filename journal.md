# Agent Build Journal

Paste this file into a new conversation to instantly restore context.

---

## What We're Building

A NanoBot/OpenClaw-style personal AI agent. Local, self-hosted, connects to
messaging platforms via independent bridge processes. Architecture is strictly
top-down: contracts → gateway → agent loop → memory → tools → bridges.

Key references:
- OpenClaw: https://openclaw.ai (Python, ~430k lines, the reference impl)
- NanoBot: https://github.com/HKUDS/nanobot (Python, ~4k lines, minimal skeleton)

---

## Architecture Overview

```
[Discord Bridge] ──→ ┐
[Matrix Bridge]  ──→ ├──→ [Gateway WS API] ──→ [SessionRouter] ──→ [Lane]
[VRM Bridge]     ──→ ┘                                                 │
                                                                   [AgentLoop]
                                                                       │
                                                         ┌─────────────┼─────────────┐
                                                    [Context]    [Inference]    [Tools]
                                                         │
                                                    [Memory/Disk]
```

**Bridges** are independent processes. They connect TO the gateway — the
gateway never spawns them. Each bridge handles its own streaming strategy
internally (buffer + send-on-final, or live edit). The gateway always streams.

**Lane** serialises messages per session (one asyncio.Queue + one worker task).
Owns one AgentLoop instance. Forwards AgentLoop's yielded reply chunks to the
gateway's dispatch method, which routes to the correct bridge reply handler.

**AgentLoop** yields OutboundReply chunks. Never calls the gateway directly.

---

## Files Implemented

### `contracts.py`
Pure data contracts. No logic, no I/O. Everything else imports from here.
- `Platform` enum: DISCORD, MATRIX
- `ContentType` enum: TEXT (media skipped for now)
- `SessionKey(platform, conversation_id)` — frozen dataclass, safe as dict key
- `UserIdentity(platform, user_id, username)`
- `InboundMessage` — canonical envelope produced by bridges
  - Fields: session_key, author, content_type, text, message_id, timestamp,
    reply_to_id (optional), trace_id (auto uuid)
- `OutboundReply` — produced by AgentLoop, consumed by bridges
  - Fields: session_key, text, reply_to_message_id, trace_id, is_partial
- `ToolCall(call_id, tool_name, args)` + `ToolCall.make()` convenience constructor
- `ToolResult(call_id, tool_name, output, is_error)` — errors are strings, not exceptions

### `context.py`
Conversation history types and context assembly pipeline.
- `HistoryEntry` dataclass — typed dialogue record for all four roles
  - Static constructors: `.user()`, `.assistant()`, `.tool_result()`, `.system()`
  - `tool_calls: list[dict]` for assistant turns that invoke tools
  - `tool_call_id: str | None` for tool result turns
- Hook stage constants: HOOK_PRE_ASSEMBLE, HOOK_FILTER_TURN,
  HOOK_TRANSFORM_TURN, HOOK_POST_ASSEMBLE
- `Context` class:
  - `register_hook(stage, fn, priority)` / `unregister_hook()`
  - `register_prompt(pid, provider_fn, role, priority)` / `unregister_prompt()`
  - `add(HistoryEntry)` — appends to dialogue, sets entry.index
  - `assemble()` → `list[dict]` — runs 4-stage pipeline, merges adjacent
    same-role turns, estimates token usage in state["tokens_used"]
  - `_render(entry)` — converts HistoryEntry to API-ready dict
  - Modules (compression, RAG, dedup) are registered externally by main.py.
    Context never loads modules itself.

### `config.py`
YAML-based config loader. Secrets always come from env vars, never the file.
- `LLMConfig(provider, model)` — `.api_key` property reads ANTHROPIC_API_KEY / OPENAI_API_KEY
- `GatewayConfig(host, port)` — defaults 127.0.0.1:8765
- `DiscordConfig(enabled)` — `.token` property reads DISCORD_TOKEN
- `MatrixConfig(enabled, homeserver, username)` — `.token` reads MATRIX_TOKEN
- `BridgesConfig(discord, matrix)`
- `MemoryConfig(workspace_path)` — resolves ~ and makes absolute in __post_init__
- `LoggingConfig(level)` — validates against known levels
- `Config` root — also has `max_tool_cycles: int = 10`
- `load(path="config.yaml") -> Config`
- `apply_logging(cfg)` — call once at startup; side-effect-free otherwise

Minimal valid config.yaml:
```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
```

### `gateway.py`
Async routing layer. No LLM calls, no tools, no memory.
- `Lane(session_key, config, reply_handler)`
  - Owns one `AgentLoop` instance (created in `__post_init__`)
  - One `asyncio.Queue[InboundMessage]`
  - `start()` — spawns `_drain()` worker task
  - `_drain()` — processes one message at a time; for each message iterates
    `loop.run(msg)` and forwards each yielded chunk to `reply_handler`
  - `stop()` — cancels worker task cleanly
- `SessionRouter(config, reply_handler)`
  - `route(msg)` — get-or-create lane, enqueue
  - `close_all()` — stops all lanes
- `Gateway(config)`
  - `register_reply_handler(platform, handler)` — bridges call this at startup
  - `push(msg)` — bridges call this to deliver inbound messages
  - `_dispatch_reply(reply)` — internal; routes OutboundReply to correct bridge handler
  - `shutdown()` — closes all lanes

---

## Dependency Graph (import rules — never reverse these)

```
contracts.py        ← no internal imports
config.py           ← no internal imports
context.py          ← contracts
agent_loop.py       ← contracts, context, config
gateway.py          ← contracts, agent_loop, config
bridges/*           ← contracts, config  (never gateway internals)
main.py             ← everything
```

---

## What's Stubbed (build these next, in order)

1. **Inference** (`agent_loop._infer`) — real LLM call using `config.llm`
2. **Tool execution** (`agent_loop._execute_tool`) — tool registry dispatch
3. **History flush** (`agent_loop._flush_history`) — write to daily Markdown log
4. **Memory layer** — workspace file loading, BM25+vector retrieval
5. **Tool registry** — declarative tools with JSON Schema + approval gate
6. **Bridge: Discord** — discord.py, buffers chunks, sends on final
7. **Bridge: Matrix** — matrix-nio, buffers chunks, sends on final
8. **Bridge: VRM/Desktop** — consumes chunks live for mouth sync
9. **Context modules** — compression, dedup, RAG (register hooks into Context)
10. **WebSocket server** — gateway exposes WS endpoint for bridges + control UI

---

## Key Design Decisions

- **Bridges own their streaming strategy.** Gateway always streams
  (is_partial chunks). Bridges buffer internally if the platform doesn't
  support streaming. VRM bridge consumes live.
- **AgentLoop yields, Lane sends.** Loop is pure — no gateway dependency,
  easy to unit test.
- **One AgentLoop per session.** Owns its own Context and history.
- **Tool errors are strings.** Executor catches exceptions, puts message in
  ToolResult.output with is_error=True. Loop sees uniform type either way.
- **Context modules are registered externally.** main.py wires up hooks and
  prompt providers at startup. Context itself never loads files or modules.
- **Filesystem is truth.** History written to Markdown after each turn.
  Vector indices are ephemeral caches that rebuild from files on startup.
- **max_tool_cycles in config.** Prevents runaway tool loops. Configurable
  without a code change.

---

## Session 2 additions

### `ai.py` (new)
Async OpenAI-compatible streaming LLM client. Uses `aiohttp`. No internal imports.
- `TextDelta(text)` — one token/chunk of streamed text
- `ToolCallAssembled(call_id, tool_name, args)` — emitted once per tool call after
  all argument fragments are assembled. Callers always get complete parseable args.
- `LLMError(message)` — connection or HTTP errors yielded, never raised
- `LLM(base_url, api_key, model, max_tokens, temperature, timeout)`
  - `stream(messages, tools) -> AsyncIterator[LLMEvent]`
  - Works with any OpenAI-compatible endpoint (Anthropic, OpenAI, OpenRouter,
    LM Studio, Ollama) — just set base_url
  - Tool call delta fragments accumulated in `tool_buf` dict keyed by index,
    emitted as `ToolCallAssembled` after stream ends

### `config.py` updated
- `LLMConfig` now has `base_url: str = "https://api.openai.com"`
- Add to config.yaml: `llm.base_url`

### `agent_loop.py` updated
- `_infer()` stub replaced with real `LLM.stream()` call
- Collects `TextDelta` chunks into `response_text`, `ToolCallAssembled` into
  `tool_calls` list, `LLMError` breaks the cycle and surfaces as reply text
- `_has_api_key()` helper checks env var without raising, used at construction
- `_execute_tool()` and `_flush_history()` remain stubs

### Updated config.yaml shape
```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  base_url: https://api.anthropic.com   # or any OpenAI-compat endpoint
```

### What's still stubbed
Same as before, minus inference (now real):
1. ~~Inference~~ — done
2. Tool execution (`_execute_tool`) — tool registry dispatch
3. History flush (`_flush_history`) — Markdown log write
4. Memory layer — workspace file loading, retrieval
5. Tool registry — declarative tools, JSON Schema, approval gate
6. Bridge: Discord
7. Bridge: Matrix
8. Bridge: VRM/Desktop
9. Context modules — compression, dedup, RAG
10. WebSocket server — gateway WS endpoint for bridges

---

## Session 3 additions

### `contracts.py` updated
- Added `Platform.CLI = "cli"`
- Added `ChatType` enum: DM | GROUP
- `SessionKey` redesigned:
  - DM sessions:    `SessionKey.dm(owner_user_id)` — platform=None, cross-platform shared
  - Group sessions: `SessionKey.group(platform, channel_id)` — platform-specific
  - `__str__`: `"dm:user-123"` or `"group:discord:chan-456"`
  - `__post_init__` validates GROUP sessions must have a platform
  - Convenience constructors: `SessionKey.dm()`, `SessionKey.group()`
- `UserIdentity` unchanged — platform field still present for display/routing

### `gateway.py` updated
- `Gateway._dm_platforms: dict[SessionKey, str]` — tracks which platform each
  DM session last received a message from
- `push()` records `msg.author.platform.value` for DM sessions on arrival
- `_dispatch_reply()` — for GROUP sessions reads platform from SessionKey;
  for DM sessions looks up `_dm_platforms`. Logs error and drops if unknown.

### `bridges/cli.py` (new)
Standalone CLI bridge using prompt_toolkit.
- `CLI_USER_ID = "cli-owner"` — stable identity, always the local user
- `CLI_SESSION = SessionKey.dm("cli-owner")` — shared DM session
- `CLIBridge(gateway)`:
  - `handle_reply(reply)` — registered with gateway; buffers `is_partial=True`
    chunks, prints full reply on `is_partial=False`
  - `run()` — async prompt loop with `patch_stdout` so prints don't clobber
    the input line; exits on Ctrl-C, EOF, "exit", or "quit"
- `main()` — loads config, creates gateway, runs bridge
- Run: `python -m bridges.cli` (requires config.yaml in working directory)

### Session routing rules
| Chat type | SessionKey | Platform on key? |
|-----------|-----------|-----------------|
| CLI DM    | `dm:cli-owner` | No — tracked in `_dm_platforms` |
| Discord DM | `dm:<discord_user_id>` | No — same session as other platforms |
| Matrix DM  | `dm:<matrix_user_id>` | No — same session if same user_id |
| Discord group | `group:discord:<channel_id>` | Yes |
| Matrix group  | `group:matrix:<room_id>` | Yes |

Note: cross-platform DM sharing requires bridges to use the same `user_id`
for the same human (e.g. a configured owner ID). This is the bridge's
responsibility — the gateway and SessionKey handle it automatically once
the user_id matches.

### What's still stubbed
1. Tool execution (`_execute_tool`) — tool registry dispatch
2. History flush (`_flush_history`) — Markdown log write
3. Memory layer — workspace file loading, retrieval
4. Tool registry — declarative tools, JSON Schema, approval gate
5. Bridge: Discord
6. Bridge: Matrix
7. Bridge: VRM/Desktop
8. Context modules — compression, dedup, RAG
9. WebSocket server — gateway WS endpoint for bridges

---

## Session 3 hotfix — config.yaml base_url

`config.py` loader now reads `llm.base_url` from the YAML file.
`config.yaml` updated to include `base_url` field under `llm`.

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  base_url: https://api.anthropic.com/v1
```

Default fallback if omitted: `https://api.openai.com`.

---

## Session 3 hotfix 2 — remove provider field

`provider` removed from `LLMConfig` entirely. It was only used to derive
which env var held the API key — replaced by explicit `api_key_env` field.

### `config.py` changes
- `LLMConfig.provider` removed
- `LLMConfig.api_key_env: str = "ANTHROPIC_API_KEY"` added
- `api_key` property reads `os.environ[self.api_key_env]`
- `load()` now raises `ValueError` if `llm.base_url` or `llm.model` missing

### `agent_loop.py` changes
- `_has_api_key()` now checks `config.llm.api_key_env` directly

### Minimal valid config.yaml
```yaml
llm:
  model: claude-sonnet-4-20250514
  base_url: https://api.anthropic.com/v1
  api_key_env: ANTHROPIC_API_KEY
```

---

## Session 3 hotfix 3 — N/A api_key_env for keyless local endpoints

`LLMConfig.api_key` property now returns `""` instead of raising when
`api_key_env` is `"N/A"` or empty. Needed for local endpoints (LM Studio,
Ollama, private inference servers) that don't require authentication.

---

## Hotfix — ai.py double /v1

`ai.py` was appending `/v1/chat/completions` to `base_url`, causing a double
`/v1/v1/chat/completions` when `base_url` already included `/v1`.
Fixed to append `/chat/completions` only. `base_url` in `config.yaml` should
always include `/v1` (or whatever version prefix the server uses).

---

## Session 3 hotfix 4 — /reset command + debug cleanup

### `ai.py`
- Removed debug `print` statements added during troubleshooting
- Note: model streams `reasoning_content` chunks before `content` — these are
  chain-of-thought tokens, correctly ignored (only `content` is picked up)

### `agent_loop.py`
- Added `reset()` — clears `Context` and resets `_turn_count`

### `gateway.py`
- `Lane.reset()` — calls `self.loop.reset()`
- `Gateway.reset_session(key)` — looks up lane by key, calls `lane.reset()`;
  no-op if session doesn't exist yet

### `bridges/cli.py`
- `/reset` command — calls `gateway.reset_session(CLI_SESSION)`, clears
  reply buffer, prints `[context cleared]`
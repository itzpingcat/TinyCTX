# TinyCTX

A lightweight agentic assistant framework. Connect it to your LLM, configure a bridge (CLI, Discord, Matrix, or HTTP), and you have a persistent, tool-using AI agent.

## Highlights

- Terminal UI with persistent session restore, slash commands, paste refs, and copy helpers
- Direct web browsing via `web_search`, `browse_url`, and `navigate`
- Branch-backed conversation history with automatic compaction when context pressure gets high
- Static + searchable memory with BM25 or embeddings, plus background memory consolidation
- Detached child branches via `spawn_agent` / `wait_agent` for bounded subagent work
- Provider presets in the CLI for OpenAI, OpenRouter, Ollama, LM Studio, `llama.cpp`, and custom OpenAI-compatible endpoints

---

> [!WARNING]
> **Security notice — read before exposing to a network.**
>
> TinyCTX gives the agent real tools: shell execution, file read/write, and web access. **Any user who can reach the bot can instruct the agent to use these tools.** By default, bridges accept messages from everyone.
>
> Before enabling any network bridge (Discord, Matrix, gateway), you must decide who is allowed to talk to the bot and configure accordingly:
>
> - **`allowed_users`** — set this in `bridges.discord.options` and `bridges.matrix.options` to a list of trusted user IDs. Any message from a user not on the list is dropped before it reaches the agent. An empty list means open access. **If you leave this empty and the bot is reachable by others, anyone can run shell commands in your workspace.**
> - **`guild_ids` / `room_ids`** — additionally restrict which servers or rooms the bot responds in.
> - **`prefix_required: true`** — in group channels, only respond when @mentioned or prefixed. This reduces noise but is not a security boundary on its own.
> - **Gateway `api_key`** — always set a strong, random key if the gateway is enabled. Never expose the gateway port to the public internet without authentication.
>
> The filesystem module sandboxes file operations to the workspace directory and maintains a shell command blacklist, but these are last-resort guardrails, not a substitute for access control. A motivated user with shell access can work around a pattern-matching blacklist.
>
> **The right mental model: treat TinyCTX like an SSH session. Only give access to people you'd give a shell to.**

---

## Installation

```bash
git clone <repo>
cd TinyCTX
pip install -r requirements.txt
cp example.config.yaml config.yaml
```

---

## Configuration

Open `config.yaml` and set at minimum:

- Your model's `base_url` and `model` name under `models:`
- `api_key_env` — the environment variable holding your API key, or `N/A` for local endpoints
- Which bridge to enable under `bridges:`
- `max_tool_cycles` — max LLM/tool round trips per turn (default: `20`)

Then export your key and run:

```bash
export ANTHROPIC_API_KEY=sk-...
python main.py
```

**`example.config.yaml` is the full configuration reference** — every option is documented there with defaults and examples.

---

## Bridges

Bridges are how users talk to the agent. Enable one or more in `config.yaml`.

### CLI

```yaml
bridges:
  cli:
    enabled: true
    options:
      quiet_startup: true      # default: hide startup/runtime INFO chatter in the terminal
      log_level: WARNING       # use INFO or inherit if you want live module logs
      compact_tools: true      # shorter tool call/result lines in the transcript
      dim_tools: true          # grey tool/progress rows so chat stands out
      word_wrap: true          # wrap transcript lines instead of hard-clipping
```

The CLI bridge is a fullscreen `rich` TUI with:

- persistent transcript restore from the last CLI session
- slash command autocomplete
- explicit copy helpers via `/copy transcript`, `/copy last-tool`, and `/copy last-error`
- paste refs like `[Pasted text #1, 332 chars]` for large or multiline clipboard content

Useful keys and commands:

- `Enter` — send message
- `Ctrl+C` — copy selected text, or copy the transcript if nothing is selected
- `Ctrl+Q` — quit TinyCTX
- `/reset` — start a fresh CLI session
- `/resume` — keep using the saved CLI session
- `/debug heartbeat` — trigger one heartbeat tick immediately
---

### Discord

```yaml
bridges:
  discord:
    enabled: true
    options:
      token_env: DISCORD_BOT_TOKEN   # env var holding the bot token
      allowed_users: [123456789]     # your Discord user ID — leave empty at your own risk
      dm_enabled: true               # allow DMs to the bot
      guild_ids: []                  # whitelist guild IDs, or empty for all
      prefix_required: true          # only respond when @mentioned or prefixed
      command_prefix: "!"            # prefix that triggers the bot in guild channels
      max_reply_length: 1900         # chunk replies longer than this
      typing_indicator: true         # show "Bot is typing..." while agent runs
```

```bash
export DISCORD_BOT_TOKEN=your-token-here
```

**Setup steps:**

1. Create a bot at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent** and **Server Members Intent** — without these the bot cannot read messages
3. Invite the bot with scopes `bot` and permissions: Read Messages, Send Messages, Read Message History

To find your Discord user ID: enable Developer Mode (Settings → Advanced → Developer Mode), then right-click your username and select "Copy User ID".

**Session routing:**
- DMs → one persistent session per user, shared across platforms
- Guild channels → one persistent session per channel

---

### Matrix

```yaml
bridges:
  matrix:
    enabled: true
    options:
      homeserver: https://your.server
      username: "@yourbot:your.server"
      password_env: MATRIX_PASSWORD        # env var holding the account password
      device_name: TinyCTX                 # device label shown in account sessions
      store_path: matrix_store             # nio key store, relative to workspace
      allowed_users: ["@you:your.server"]  # your MXID — leave empty at your own risk
      dm_enabled: true                     # respond in 1-on-1 rooms
      room_ids: []                         # whitelist room IDs, or empty for all joined rooms
      prefix_required: true                # in group rooms, only respond when @mentioned or prefixed
      command_prefix: "!"
      max_reply_length: 16000
      sync_timeout_ms: 30000
```

```bash
export MATRIX_PASSWORD=your-password-here
```

DM detection is based on room member count (2 = DM). In group rooms the bot only responds when @mentioned or when the message starts with `command_prefix` (if `prefix_required: true`).

For E2EE support set `encryption_enabled=True` in the bridge source.

**Session routing:**
- 1-on-1 rooms → `SessionKey.dm(sender_mxid)` — platform-agnostic
- Group rooms → `SessionKey.group(Platform.MATRIX, room_id)`

---

### HTTP / SSE gateway

For external clients (custom scripts, SillyTavern, etc.):

```yaml
gateway:
  enabled: true
  host: 127.0.0.1
  port: 8080
  api_key: "your-secret-token"
```

Send messages:

```bash
curl -N http://localhost:8080/v1/sessions/alice/message \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello!", "stream": true}'
```

Keep the gateway bound to `127.0.0.1` unless you have a specific reason to expose it, and always set a strong `api_key`. See `CLAUDE.md → Gateway API` for the full endpoint reference.

---

## Workspace

The workspace is a directory on disk where the agent keeps its persistent state. Default: `~/.tinyctx`. Change it in `config.yaml`:

```yaml
workspace:
  path: ~/.tinyctx
```

Layout:

```
~/.tinyctx/
├── agent.db       # Full branch-backed conversation tree
├── cursors/       # Per-bridge/session cursors (CLI resume uses this)
├── SOUL.md        # Agent personality — loaded first, every turn
├── AGENTS.md      # Sub-agent or persona definitions
├── MEMORY.md      # Long-term facts always in context
├── memory/        # Semantic search corpus — any *.md files here are searchable
│   ├── session-YYYY-MM-DD.md   # Session notes written by the agent
│   └── ...
├── uploads/       # Files and images sent by users via bridges; agent can read these
├── CRON.json      # Scheduled jobs (cron module)
└── skills/        # Skill folders
    └── mytool/
        └── SKILL.md
```

Edit these files any time — they're re-read every turn, no restart needed.

TinyCTX does not keep chat state only in RAM. Conversations are stored in `agent.db` as a branch tree, and the CLI bridge restores the visible transcript from the saved cursor on startup.

---

## Memory

The memory module gives the agent access to your knowledge base.

**Static files** — always injected into context:
- `SOUL.md` — who the agent is
- `AGENTS.md` — roles, personas, or sub-agent definitions
- `MEMORY.md` — facts that should always be available

**Semantic search** — any `.md` files placed under `workspace/memory/` are indexed and searched automatically each turn. The most relevant chunks are injected into context. Subdirectories are supported.

To enable search, add an embedding model to your config:

```yaml
models:
  embed:
    kind: embedding
    base_url: http://localhost:11434/v1
    api_key_env: N/A
    model: nomic-embed-text

memory_search:
  embedding_model: embed
```

Without an embedding model, BM25 keyword search is used instead — no embedding server required.

The agent also has a `memory_search` tool it can call explicitly to look things up on demand.

See `example.config.yaml` under `memory_search:` for all options (chunk strategy, budget, top-k, auto-inject, etc.).

When the turn grows enough to threaten context budget, TinyCTX can also spawn a background memory-consolidation branch instead of stuffing more memory inline.

---

## Conversation Compaction

TinyCTX persists the full conversation tree in `agent.db`, but it does not blindly replay every old message forever. When the active turn gets close to the configured context limit, the agent compacts older history into a summary boundary and keeps the most recent dialogue raw.

This means:

- older work is still preserved on disk
- active prompt assembly stays smaller and faster
- the CLI can resume prior sessions without needing the full raw transcript in-context every turn

Compaction is automatic; no extra configuration is required beyond setting a sane `context:` limit for your model.

---

## Skills

Skills are reusable instruction sets the agent can load on demand. Place a folder containing a `SKILL.md` file anywhere under `workspace/skills/` (or another configured skills directory).

The agent sees a compact index of available skills in its system prompt and calls `use_skill("name")` to load the full instructions when it needs them.

Skills follow the [agentskills.io](https://agentskills.io) convention. Any skill written to that standard works here.

---

## Subagents

TinyCTX can spawn bounded detached child branches for parallel side work:

- `spawn_agent(prompt="...")`
- `wait_agent(task_id="...")`

Subagents run as child `AgentLoop`s on their own branches and report back by task ID. They are capped by `subagents.max_concurrent` and completed handles are pruned after `subagents.completed_ttl_seconds`.

Example config:

```yaml
subagents:
  max_concurrent: 4
  completed_ttl_seconds: 900
```

Use them for isolated side tasks, not trivial work you can finish faster in the current turn.

---

## Tools

The following tools are available to the agent out of the box (if the corresponding module is enabled):

| Tool | What it does |
|------|-------------|
| `shell` | Run a shell command in the workspace directory |
| `view` | Read a file with line numbers, or list a directory |
| `write_file` | Create or write to a file (append, prepend, overwrite) |
| `str_replace` | Edit an existing file by replacing a string (`replace_all` supported) |
| `grep` | Search file contents with regex (ripgrep with Python fallback) |
| `glob_search` | Find files by name pattern, sorted by modification time |
| `web_search` | Search the web via DuckDuckGo |
| `browse_url` | Fetch and scrape a specific URL; supports text or HTML output, optional JS rendering, and an optional `prompt` for focused extraction |
| `navigate` | Open a page in the browser automation session and return a compact element map |
| `memory_search` | Search the semantic memory index |
| `spawn_agent` | Start a detached subagent on a child branch |
| `wait_agent` | Wait for a spawned subagent to finish or poll its current status |
| `use_skill` | Load a skill by name |
| `todo_write` | Update the session task checklist (for multi-step work) |
| `todo_read` | View the current task list |

Write tools (`write_file`, `str_replace`) require the file to have been read first via `view()` — this prevents blind overwrites and catches stale edits from external changes.

Modules are enabled automatically if their directory exists under `modules/`. No configuration needed beyond having the right dependencies installed.

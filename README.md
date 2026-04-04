# TinyCTX

A lightweight agentic assistant framework. Connect it to your LLM, configure a bridge (CLI, Discord, Matrix, or HTTP), and you have a persistent, tool-using AI agent.

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
```

Type messages directly in the terminal. `/reset` clears the session, `exit` quits. No extra dependencies.

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

---

## Skills

Skills are reusable instruction sets the agent can load on demand. Place a folder containing a `SKILL.md` file anywhere under `workspace/skills/` (or another configured skills directory).

The agent sees a compact index of available skills in its system prompt and calls `use_skill("name")` to load the full instructions when it needs them.

Skills follow the [agentskills.io](https://agentskills.io) convention. Any skill written to that standard works here.

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
| `memory_search` | Search the semantic memory index |
| `use_skill` | Load a skill by name |

Write tools (`write_file`, `str_replace`) require the file to have been read first via `view()` — this prevents blind overwrites and catches stale edits from external changes.

Modules are enabled automatically if their directory exists under `modules/`. No configuration needed beyond having the right dependencies installed.

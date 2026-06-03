# TinyCTX

A context-efficient agentic assistant framework. Connect it to your LLM, configure a bridge (CLI, Discord, Matrix, or HTTP gateway), and you have a persistent, tool-using AI agent with memory consolidation, a knowledge graph, user permissions, scheduled heartbeats, subagent support, and web browsing.

## Highlights

- Effortless onboarding wizard
- Optimised for local LLMs — 32k context recommended, 16k workable
- Branch-backed conversation tree persisted in SQLite — no state lost on restart
- Active memory consolidation and background knowledge-graph extraction (LadybugDB)
- Semantic search over your notes: BM25 or hybrid BM25 + embeddings
- User permission system (0–100) with per-bridge role/power-level mapping
- Terminal UI with persistent session restore, slash commands, paste refs, and copy helpers
- Direct web browsing via `web_search` and `open_url` (Playwright, headless by default)
- Proactive heartbeat and cron system
- Subagent support (`spawn_agent` / `wait_agent`)
- MCP server integration
- Provider presets for OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp, and any OpenAI-compatible endpoint

---

> [!WARNING]
> **Security notice — read before exposing to a network.**
>
> TinyCTX gives the agent real tools: shell execution, file read/write, and web access. **Any user who can reach the bot can instruct the agent to use these tools.** By default, bridges accept messages from everyone.
>
> Before enabling any network bridge (Discord, Matrix, gateway), decide who is allowed to talk to the bot and configure accordingly:
>
> - **`allowed_users_dm`** (Discord) / **`allowed_users`** (Matrix) — allowlists of trusted user IDs. Messages from users not on the list are dropped before reaching the agent. An empty list means open access. **If you leave this empty and the bot is reachable by others, anyone can run shell commands in your workspace.**
> - **`allowed_servers` / `room_ids`** — additionally restrict which servers or rooms the bot responds in.
> - **Permission levels** — new users start at `default_permission` (default: 25). Tools have `min_permission` thresholds; a caller cannot execute tools above their level. Grant higher levels with `/user grant <username> <level>` (requires level 100).
> - **`prefix_required: true`** — in group channels, only respond when @mentioned or prefixed. This reduces noise but is not a security boundary on its own.
> - **Gateway `api_key`** — always set a strong, random key if the gateway is enabled. Never expose the gateway port to the public internet without authentication.
>
> The filesystem module sandboxes file operations to the workspace directory and maintains a shell command blacklist, but these are last-resort guardrails, not a substitute for access control.
>
> **The right mental model: treat TinyCTX like an SSH session. Only give access to people you'd give a shell to.**

---

## Installation

```bash
git clone https://github.com/itzpingcat/TinyCTX
cd TinyCTX
pip install -e .
python -m TinyCTX onboard
```

This starts the interactive configuration wizard. It will walk you through choosing a provider, configuring your workspace, and optionally setting up bridges.

## Workspace

The workspace is a directory on disk where the agent keeps its persistent state. Default: `~/.tinyctx`. Change it in `config.yaml` or during onboarding.

Layout:

```
~/.tinyctx/
├── agent.db          # Branch-backed conversation tree (SQLite WAL)
├── cursors/          # Per-bridge/session cursors (CLI resume uses this)
├── SOUL.md           # Agent personality — loaded first, every turn
├── AGENTS.md         # Sub-agent or persona definitions
├── MEMORY.md         # Long-term facts always in context
├── EM.md             # Equipment manifest (optional; templated with OS/date/paths)
├── HEARTBEAT.md      # Heartbeat instructions (read by agent each heartbeat turn)
├── memory/           # Semantic search corpus — any *.md files here are searchable
│   ├── graph.lbug    # LadybugDB knowledge graph (memory module)
│   └── session-YYYY-MM-DD.md
├── downloads/        # Files and images sent by users via bridges
├── CRON.json         # Scheduled jobs (cron module)
└── skills/           # Skill folders
    └── mytool/
        └── SKILL.md
```

Edit these files any time — they are re-read every turn, no restart needed.

TinyCTX does not keep chat state only in RAM. Conversations are stored in `agent.db` as a branch tree, and the CLI bridge restores the visible transcript from the saved cursor on startup.

---

## Context Budget

TinyCTX is designed to work within a fixed context window rather than silently discarding history. Set `context:` in `config.yaml` to match your model:

```yaml
context: 32768   # recommended; 16384 works for smaller models
```

When the active turn approaches this limit, TinyCTX trims the oldest non-system turns. The memory and RAG modules then pick up the slack — important facts are preserved in the knowledge graph or semantic index and re-injected as needed. The full conversation tree is always on disk.

---

## Memory

TinyCTX has two complementary memory systems.

### RAG (workspace/memory/*.md)

Any `.md` files placed under `workspace/memory/` are indexed and searched automatically each turn. The most relevant chunks are injected into context. Subdirectories are supported.

**Static files** — always injected every turn:
- `SOUL.md` — agent personality
- `AGENTS.md` — roles, personas, or sub-agent definitions
- `MEMORY.md` — facts that should always be available

To enable embedding-based (semantic) search, add an embedding model:

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

Without an embedding model, BM25 keyword search is used — no extra server required.

The agent can also call `memory_search` explicitly to look things up on demand. See `example.config.yaml` under `memory_search:` for all options (chunk strategy, budget, top-k, auto-inject, etc.).

### Knowledge Graph (memory module)

The `memory` module adds a property-graph knowledge store backed by **LadybugDB**. A background librarian process walks unvisited conversation nodes (tracked with DB flags), extracts entities and relationships via sub-agents, and writes them to `memory/graph.lbug`. The main agent reads the graph via `kg_search`, `kg_traverse`, and `call_librarian` tools. Pinned entities are injected into the system prompt automatically.

```yaml
# memory module (all optional — these are the defaults)
# graph_path:             memory/graph.lbug
# trigger_interval_hours: 6
# batch_size:             20
# embedding_model:        ""    # empty = keyword-only graph search
# memory_block_tokens:    4096
# librarian_model:        ""    # empty = use primary LLM
```

---

## User Permissions

Every inbound message is associated with a **User** — a TinyCTX-internal account that may have identities on multiple platforms (Discord, Matrix, CLI, …). Users are created automatically on first contact and stored in `~/.config/tinyctx/users.db`.

Each user has a **permission level** (0–100). Tools declare a `min_permission` threshold; a caller cannot execute tools above their level. New users start at `default_permission` (default: 25).

### Managing users

```
/user grant <username> <level>   — requires caller level 100
/user info  <username>
/user rename <username> <new>
```

### Bridge permission mapping

**Discord** — maps role IDs to permission levels:

```yaml
bridges:
  discord:
    options:
      dm_permission: 50           # level granted to anyone who DMs the bot
      default_permission: 25      # fallback for users with no matching role
      role_permissions:
        123456789012345678: 100   # Admin role → level 100
        234567890123456789: 50    # Moderator → level 50
```

**Matrix** — maps room power levels:

```yaml
bridges:
  matrix:
    options:
      default_permission: 25
      power_level_map:
        100: 100
        50:  50
        0:   25
```

### Token visibility

By default (`permissions.minimal_tokens: true`), the LLM only sees tools the current caller has permission to execute — higher-privilege tools are hidden entirely, saving tokens and avoiding confusion. Set `minimal_tokens: false` to show all tools; execution-time guards still apply.

---

## Skills

Skills are reusable instruction sets the agent can load on demand. Place a folder containing a `SKILL.md` file anywhere under `workspace/skills/`.

The agent sees a compact index of available skills and calls `use_skill("name")` to load the full instructions when needed. Skills follow the [agentskills.io](https://agentskills.io) convention.

---

## Subagents

TinyCTX can spawn bounded child branches for parallel side work:

```
spawn_agent(prompt="…")    — start a detached subagent
wait_agent(task_id="…")    — wait for it to finish or poll status
```

Good for isolated side tasks; not worth the overhead for trivial work you can finish in the current turn.

---

## MCP Servers

Any stdio MCP server can be wired in via config:

```yaml
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

Per-tool visibility: `always_on` | `deferred` (hidden until `tools_search` enables it) | `disabled`. All tools default to `deferred` if no `tools:` block is present.

---

## Tools

| Tool | What it does |
|------|-------------|
| `shell` | Run a shell command in the workspace directory |
| `view` | Read a file with line numbers, or list a directory |
| `write_file` | Create or write to a file (append, prepend, overwrite) |
| `edit_file` | Edit an existing file by replacing a string |
| `grep` | Search file contents with regex (ripgrep with Python fallback) |
| `glob_search` | Find files by name pattern, sorted by modification time |
| `web_search` | Search the web via DuckDuckGo |
| `open_url` | Open any URL in a Playwright browser; `headless=False` for captchas |
| `memory_search` | Search the RAG semantic index |
| `kg_search` | Search the LadybugDB knowledge graph |
| `kg_traverse` | Traverse graph relationships from a starting entity |
| `call_librarian` | Trigger on-demand knowledge-graph extraction |
| `spawn_agent` | Start a detached subagent on a child branch |
| `wait_agent` | Wait for a spawned subagent to finish or poll its status |
| `use_skill` | Load a skill by name |
| `todo_write` | Update the session task checklist |
| `todo_read` | View the current task list |
| `present` | Deliver files to the user via the active bridge |
| `tools_search` | BM25 search over available tools; enables matching deferred tools |

Write tools (`write_file`, `edit_file`) require the file to have been read first via `view()` — this prevents blind overwrites.

---

## Configuration Reference

Full annotated config: see `example.config.yaml`. Key top-level keys:

| Key | Default | Purpose |
|-----|---------|---------|
| `context` | `16384` | Token budget (recommend `32768`) |
| `max_tool_cycles` | `10` | Max tool-call iterations per turn |
| `workspace.path` | `~/.tinyctx` | Workspace directory |
| `llm.primary` | — | Primary model name (must be `kind: chat`) |
| `llm.fallback` | `[]` | Fallback model names, tried in order |
| `permissions.minimal_tokens` | `true` | Hide tools the caller cannot use |
| `gateway.api_key` | — | Auth token for the HTTP gateway |

Models are defined under `models:` with `kind: chat` (default) or `kind: embedding`. Embedding models are never used for LLM routing.

---

## Bridges

### CLI

Interactive terminal session with Rich TUI, slash commands, and persistent session restore.

```bash
tinyctx launch cli
```

### Discord

```yaml
bridges:
  discord:
    enabled: true
    options:
      token_env: DISCORD_BOT_TOKEN
      allowed_users_dm: [123456789012345678]
      allowed_servers:
        987654321098765432: []   # all channels in this server
      admin_users: [123456789012345678]
      dm_permission: 50
      default_permission: 25
      role_permissions:
        111111111111111111: 100
      prefix_required: true
      command_prefix: "!"
```

Required bot intents: **Message Content**, **Server Members**. Required permissions: Read Messages, Send Messages, Read Message History.

### Matrix

```yaml
bridges:
  matrix:
    enabled: true
    options:
      homeserver: https://matrix.org
      username: "@yourbot:matrix.org"
      password_env: MATRIX_PASSWORD
      allowed_users: ["@you:matrix.org"]
      default_permission: 25
      power_level_map:
        100: 100
        50:  50
        0:   25
```

Requires `matrix-nio` (`pip install matrix-nio`, or `matrix-nio[e2e]` for E2EE).

### Gateway (HTTP/SSE)

OpenAI-compatible `/v1/chat` endpoint with SSE streaming. Useful for external clients and SillyTavern.

```yaml
gateway:
  enabled: true
  host: 127.0.0.1
  port: 8085
  api_key: "your-secret-token"
```

---

## CLI Commands

```bash
tinyctx onboard    # first-run setup wizard
tinyctx start      # daemonise the server
tinyctx stop       # stop the daemon
tinyctx status     # check if running
tinyctx launch cli # attach an interactive terminal session
```

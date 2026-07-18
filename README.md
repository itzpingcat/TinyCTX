# TinyCTX

A context-efficient agentic assistant framework. Connect it to your LLM, configure a bridge (CLI, Discord, Matrix, or HTTP gateway), and you have a persistent, tool-using AI agent with memory consolidation, a knowledge graph, user permissions, scheduled heartbeats, subagent support, and web browsing.

## Highlights

- Effortless onboarding wizard
- Optimised for local LLMs - 32k context recommended, 16k workable
- Branch-backed conversation tree persisted in SQLite - no state lost on restart
- Active memory consolidation and background knowledge-graph extraction (LadybugDB)
- Semantic search over your notes: BM25 or hybrid BM25 + embeddings
- User permission system (0-100) managed via the internal user registry
- Terminal UI with persistent session restore, slash commands, paste refs, and copy helpers
- Direct web browsing via `web_search` and `open_url` (Playwright, headless by default)
- Proactive heartbeat and cron system
- Subagent support (`spawn_agent` / `wait_agent`)
- MCP server integration
- Provider presets for OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp, and any OpenAI-compatible endpoint

---

> [!WARNING]
> **Security notice - read before exposing to a network.**
>
> TinyCTX gives the agent real tools: shell execution, file read/write, and web access. **Any user who can reach the bot can instruct the agent to use these tools.** By default, bridges accept messages from everyone.
>
> Before enabling any network bridge (Discord, Matrix, gateway), decide who is allowed to talk to the bot and configure accordingly:
>
> - **`allowed_servers` / `room_ids`** - restrict which servers or rooms the bot responds in. An empty `allowed_servers` means the bot won't respond in any server.
> - **Permission levels** - every user has a level (0-100) stored in the TinyCTX user registry. New users start at 0. Tools declare a `min_permission` threshold; callers cannot execute tools above their level. Grant higher levels with `/user grant <username> <level>` (requires level 100). Set `dm_requires_permission` and `reset_requires_permission` in the Discord bridge options to control who can DM the bot and who can `/reset`.
> - **`prefix_required: true`** - in group channels, only respond when @mentioned or prefixed. This reduces noise but is not a security boundary on its own.
> - **Gateway `api_key`** - always set a strong, random key if the gateway is enabled. Never expose the gateway port to the public internet without authentication.
>
> The filesystem module sandboxes `write_file`/`edit_file` to the workspace directory only. `view`/`grep`/`glob_search` can additionally see any directory listed in `filesystem.read_only_paths` in config.yaml (read-only, never writable - e.g. `/app` for the agent's own source code in the container). Nothing outside workspace + that whitelist is reachable at all. The module also maintains a shell command blacklist, but these are last-resort guardrails, not a substitute for access control.
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

## Instance Directory

An *instance* is a self-contained directory holding one agent's config, workspace, and internal data - everything an agent needs lives in one place, so running multiple agents is just running multiple instance directories.

Every `tinyctx` command resolves the instance directory the same way:

1. `--dir PATH`, if given
2. The nearest ancestor of your current directory that's literally named `.tinyctx` (so running from inside `<instance>/workspace/skills/foo` still resolves correctly)
3. A `.tinyctx/` child of your current directory
4. Fallback: `~/.tinyctx`

```
<instance>/
+-- config.yaml           # loaded from here by default
+-- workspace/            # agent-authored content - visible to the agent's own filesystem tools
|   +-- SOUL.md           # Agent personality - loaded first, every turn
|   +-- AGENTS.md         # Sub-agent or persona definitions
|   +-- EM.md             # Equipment manifest (optional; templated with OS/date/paths)
|   +-- HEARTBEAT.md      # Heartbeat instructions (read by agent each heartbeat turn)
|   +-- rag/, memory/*.md # Semantic search corpus - any *.md files here are searchable
|   +-- downloads/        # Files and images sent by users via bridges
|   +-- CRON.json         # Scheduled jobs (cron module)
|   +-- skills/
|       +-- mytool/
|           +-- SKILL.md
+-- data/                 # TinyCTX-internal state - NOT visible to the agent's own filesystem tools
    +-- agent.db          # Branch-backed conversation tree (SQLite WAL)
    +-- users.db          # User registry
    +-- cursors/          # Per-bridge/session cursors (CLI resume uses this)
    +-- memory/
        +-- graph.lbug    # LadybugDB knowledge graph (memory module)
        +-- librarian.log # Librarian logging
        +-- dedup_cache.db
```

`workspace.path` and `data.path` both default to `<instance>/workspace` and `<instance>/data` - relative to wherever `config.yaml` itself lives - so a fresh config.yaml doesn't need to state either explicitly. Override only if you want something non-standard.

Edit files under `workspace/` any time - they are re-read every turn, no restart needed. Files under `data/` are internal state the agent's own tools can't reach; edit them only if you know what you're doing.

TinyCTX does not keep chat state only in RAM. Conversations are stored in `data/agent.db` as a branch tree, and the CLI bridge restores the visible transcript from the saved cursor on startup.

---

## Context Budget

TinyCTX is designed to work within a fixed context window rather than silently discarding history. Set `context:` in `config.yaml` to match your model:

```yaml
context: 32768   # recommended; 16384 works for smaller models
```

When the active turn approaches this limit, TinyCTX trims the oldest non-system turns. The memory and RAG modules then pick up the slack - important facts are preserved in the knowledge graph or semantic index and re-injected as needed. The full conversation tree is always on disk.

---

## Memory

TinyCTX has three complementary memory systems.

### Core Files

These files are always injected every turn:
- `SOUL.md` - agent personality
- `AGENTS.md` - roles, personas, or sub-agent definitions
- `TOOLS.md` - tool usage guidelines

### RAG (workspace/rag/folder/*.md)

Any `.md` files placed under `workspace/rag` are indexed. They can be configured to be searched automatically each turn. The most relevant chunks are injected into context. Subdirectories are supported.

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

Without an embedding model, BM25 keyword search is used - no extra server required.

The agent can also call `rag_search` explicitly to look things up on demand. See `example.config.yaml` under `rag:` for all options (chunk strategy, budget, top-k, auto-inject, etc.).

### Knowledge Graph (memory module)

The `memory` module adds a property-graph knowledge store backed by **LadybugDB**. A background librarian process walks unvisited conversation nodes (tracked with DB flags), extracts entities and relationships via sub-agents, and writes them to `data/memory/graph.lbug`. The main agent reads the graph via `kg_search`, `kg_traverse`, and `call_librarian` tools. Pinned entities are injected into the system prompt automatically.

```yaml
# memory module (all optional - these are the defaults)
# graph_path:             data/memory/graph.lbug   # resolved relative to data.path, not workspace.path
# trigger_interval_hours: 6
# batch_size:             20
# embedding_model:        ""    # empty = keyword-only graph search
# memory_block_tokens:    4096
# librarian_model:        ""    # empty = use primary LLM
```

---

## User Permissions

Every inbound message is associated with a **User** - a TinyCTX-internal account that may have identities on multiple platforms (Discord, Matrix, CLI, etc). Users are created automatically on first contact and stored in `<instance>/data/users.db`.

Each user has a **permission level** (0-100). Tools declare a `min_permission` threshold; a caller cannot execute tools above their level. New users start at `default_permission` (default: 25).

### Managing users

```
/user grant <username> <level>   - requires caller level 100
/user info  <username>
/user rename <username> <new>
```

### Tool visibility

By default (`permissions.minimal_tokens: true`), the LLM only sees tools the current caller has permission to execute - higher-privilege tools are hidden entirely, saving tokens and avoiding confusion. Set `minimal_tokens: false` to show all tools; execution-time guards still apply.

---

## Skills

Skills are reusable instruction sets the agent can load on demand. Place a folder containing a `SKILL.md` file anywhere under `workspace/skills/`.

The agent sees a compact index of available skills and calls `use_skill("name")` to load the full instructions when needed. Skills follow the [agentskills.io](https://agentskills.io) convention.

---

## Subagents

TinyCTX can spawn bounded child branches for parallel side work:

```
spawn_agent(prompt="...")    - start a detached subagent
wait_agent(task_id="...")    - wait for it to finish or poll status
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

Write tools (`write_file`, `edit_file`) require the file to have been read first via `view()` - this prevents blind overwrites.

---

## Configuration Reference

Full annotated config: see `example.config.yaml`. Key top-level keys:

| Key | Default | Purpose |
|-----|---------|---------|
| `context` | `16384` | Token budget (recommend `32768`) |
| `max_tool_cycles` | `10` | Max tool-call iterations per turn |
| `workspace.path` | `<instance>/workspace` | Agent-visible working directory |
| `data.path` | `<instance>/data` | Internal state (agent.db, users.db, memory graph, cursors) - not visible to the agent's own filesystem tools |
| `filesystem.read_only_paths` | `[]` | Extra directories `view`/`grep`/`glob_search` can see (never write to) - e.g. `/app` |
| `llm.primary` | - | Primary model name (must be `kind: chat`) |
| `llm.fallback` | `[]` | Fallback model names, tried in order |
| `permissions.minimal_tokens` | `true` | Hide tools the caller cannot use |
| `gateway.api_key` | - | Auth token for the HTTP gateway |
| `gateway.port` | `8085` | Overridable per-instance via `TINYCTX_PORT` env (set automatically by `tinyctx start`) |

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
      allowed_servers:
        987654321098765432: []   # all channels in this server
      dm_enabled: true
      dm_requires_permission: 75    # minimum user registry level to DM the bot
      reset_requires_permission: 75 # minimum level to /reset in a server
      prefix_required: true
      command_prefix: "!"
```

Required bot intents: **Message Content**, **Server Members**. Required permissions: Read Messages, Send Messages, Read Message History.

### Telegram

```yaml
bridges:
  telegram:
    enabled: true
    options:
      token_env: TELEGRAM_BOT_TOKEN   # env var holding the @BotFather token
      allowed_users: [123456789]      # Telegram user IDs; empty = open to all
      max_reply_length: 4096          # Telegram's hard per-message limit
      mention_aliases: ["eve"]        # extra names the bot answers to in groups
```

Create a bot with [@BotFather](https://t.me/BotFather) and export the token as `TELEGRAM_BOT_TOKEN`. Run the bridge like the others: `python -m TinyCTX.bridges.telegram`. In groups the bot answers to its @username, its BotFather display name, a reply to one of its own messages, and any `mention_aliases`; bare-name matching requires privacy mode **disabled** in @BotFather (`/setprivacy`, then Disable, then re-add the bot to the group).

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
tinyctx onboard      # first-run setup wizard for the resolved instance
tinyctx start        # start the stack (docker compose) for the resolved instance
tinyctx stop         # stop it
tinyctx status       # check if running
tinyctx launch cli   # attach an interactive terminal session
```

All five accept `--dir PATH` to target a specific `.tinyctx` instance directory instead of relying on autodetection - this is how you run more than one agent on the same machine: give each its own instance directory (e.g. `tinyctx onboard --dir ~/agent-b/.tinyctx`), then use `--dir` (or `cd` into it) for every subsequent command against that instance.

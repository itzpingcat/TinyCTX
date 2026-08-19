# TinyCTX

> [!NOTE]
> **TinyCTX is in Beta.** Interfaces, config keys, and internal storage formats may still change between versions. Back up your instance directory before upgrading.

A context-efficient agentic assistant framework. Connect it to your LLM, configure a bridge (CLI, Discord, Telegram, Matrix, or HTTP gateway), and you have a persistent, tool-using AI agent with memory consolidation, a knowledge graph, a capability-based permission system, scheduled cron jobs, subagent support, and web browsing.

## Highlights

- Effortless onboarding wizard
- Optimised for local LLMs - 32k context recommended, 16k workable
- Branch-backed conversation tree persisted in SQLite - no state lost on restart
- Active memory consolidation and background knowledge-graph extraction (LadybugDB)
- Semantic search over your notes: BM25 or hybrid BM25 + embeddings
- Named-capability permission system, checked centrally against a declarative per-tool/per-command requirement
- Terminal UI with persistent session restore, slash commands, paste refs, and copy helpers
- Web browsing via `web_search`, `open_url`, and Camoufox-based browser automation (click, type, extract, screenshot)
- Cron-scheduled agent turns, backed by a SQLite store outside the agent's own filesystem reach
- Subagent support (`spawn_agent` / `wait_agent`)
- Tool discovery: BM25 + vector search over the tool registry, with passive auto-enable and an explicit `tools_search` tool
- MCP server integration
- Provider presets for OpenAI, OpenRouter, Ollama, LM Studio, llama.cpp, and any OpenAI-compatible endpoint

---

> [!WARNING]
> **Security notice - read before exposing to a network.**
>
> TinyCTX gives the agent real tools: shell execution, file read/write, and web access. **Any user who can reach the bot can instruct the agent to use these tools.** By default, bridges accept messages from everyone.
>
> Before enabling any network bridge (Discord, Telegram, Matrix, gateway), decide who is allowed to talk to the bot and configure accordingly:
>
> - **`allowed_servers` / `room_ids` / `allowed_users`** - restrict which servers, rooms, or users the bot responds to. An empty `allowed_servers` means the bot won't respond in any server.
> - **Capability-based permissions** - each user's authority is a set of named capabilities (file read/write, network read/write, shell execution location, cron creation, and so on), not a single number. Tools and slash commands each declare the capabilities they require; a caller missing any of them is denied. A single shared `permissions.template` in `config.yaml` sets the default grant for every user; grant more to specific users via `permission_overrides`.
> - **`prefix_required: true`** - in group channels, only respond when @mentioned or prefixed. This reduces noise but is not a security boundary on its own.
> - **Gateway `api_key`** - always set a strong, random key if the gateway is enabled. Never expose the gateway port to the public internet without authentication.
>
> The filesystem module sandboxes `write_file`/`edit_file` to the workspace directory only. `view`/`grep`/`glob_search` can additionally see any directory listed in `filesystem.read_only_paths` in config.yaml (read-only, never writable - e.g. `/app` for the agent's own source code in the container). Nothing outside workspace + that whitelist is reachable at all. The `shell` tool runs in an isolated sandbox container by default (no LAN access); running it in the main container instead requires the `backend_exec` capability. Commands are parsed with tree-sitter-bash, classified into the capability bools they need, and checked centrally - a structural shape policy underneath rejects `$()`, unquoted globs used as commands, and unrecognized bash syntax as a last-resort guardrail, not a substitute for access control.
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
|   +-- TOOLS.md          # Tool usage guidelines
|   +-- EM.md             # Equipment manifest (optional; templated with OS/date/paths)
|   +-- rag/, memory/*.md # Semantic search corpus - any *.md files here are searchable
|   +-- downloads/        # Files and images sent by users via bridges
|   +-- outputs/browser/  # Camoufox screenshots
|   +-- skills/
|       +-- mytool/
|           +-- SKILL.md
+-- data/                 # TinyCTX-internal state - NOT visible to the agent's own filesystem tools
    +-- agent.db          # Branch-backed conversation tree (SQLite WAL)
    +-- users.db          # User registry and permission overrides
    +-- cron.db           # Scheduled jobs (cron module)
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

## Permissions

Every inbound message is associated with a **User** - a TinyCTX-internal account that may have identities on multiple platforms (Discord, Telegram, Matrix, CLI, etc). Users are created automatically on first contact and stored in `<instance>/data/users.db`.

Authority is a set of named capabilities, not a single number. Each capability is a narrow, independently gate-able bool: `file_read`, `file_write`, `network_read`, `network_write`, `backend_exec` (run tools in the main container instead of the sandbox), `untrusted_exec`, `manage_ctx`, `model_swap`, `memory_read`, `memory_write`, `cron_create`, `cron_admin`, `user_read`, `root`, `dm_access`, `equipment_trusted`, and `image_gen`. `root` is a deliberate catch-all meaning "administer the instance itself" (edit anyone's permissions, rename or merge users, shut the gateway down) - it does not imply the other capabilities, so grant it explicitly alongside anything else a fully-elevated user needs.

Tools and slash commands each declare the capability set they require. A caller is only allowed to invoke a tool or command if their effective capability set covers what it declares; the check runs centrally, once, before execution. Network capabilities are split by effect rather than byte direction - `network_write` covers anything with a durable remote effect (POST/PUT/PATCH/DELETE-shaped calls) and implies `network_read`, since any such call also returns a response body.

A single `permissions.template` in `config.yaml` sets the default grant shared by every user (omitting it defaults to fully closed). Grant more to a specific user with `permission_overrides` on their record, via `/user`-style admin commands, `onboard/fix_permissions.py`, or the gateway's user-elevation endpoint.

### Tool visibility

By default (`permissions.minimal_tokens: true`), the LLM only sees tools the current caller has permission to execute - higher-privilege tools are hidden entirely, saving tokens and avoiding confusion. Set `minimal_tokens: false` to show all tools; execution-time guards still apply.

### Tool discovery (tool RAG)

Beyond visibility filtering, TinyCTX ranks the tool registry itself so large tool sets don't have to sit fully expanded in context. Every registered tool is embedded (BM25 always; vector search when an embedding model is configured) and re-synced each turn against a content hash, so unchanged tools cost nothing to re-rank. Two paths use the same ranking:

- **Passive search** - runs automatically each turn and silently enables the top-scoring tools for the model before it sees its tool list.
- **`tools_search`** - an explicit tool the model can call to list candidates matching a query; a matching tool must still be enabled with a follow-up call before it can be invoked.

Both fuse BM25 and vector results via reciprocal-rank fusion. See `example.config.yaml` under `tools:` for auto-enable limits, score thresholds, and the embedding model used for tool ranking.

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

## Cron

The `cron` module lets the agent schedule its own future turns. Jobs are stored in a SQLite database under the instance's internal data dir (`data/cron.db`) rather than in `workspace/`, so the agent's own filesystem tools cannot create or edit jobs directly. A schedule is a single cron expression (evaluated via `croniter`); a one-shot reminder is just a cron expression matching one specific future minute rather than a separate schedule shape.

Jobs are created, listed, and removed only through the `add_cron`, `list_cron`, and `remove_cron` tools:

- `add_cron` runs as the calling user's real identity and channel, and requires the caller hold `cron_create`. A job only fires if its *stored creator* currently holds `cron_create` - this is re-checked every run, not just at creation, so revoking the capability later disables the job's future runs outright rather than letting it run half-privileged.
- `list_cron` / `remove_cron` only see or act on jobs created in the caller's own channel; `remove_cron` additionally requires being the job's creator, or holding `cron_admin`.

Job output is delivered back to the originating channel through the same per-platform renderer a live turn uses.

```yaml
cron:
  store_file: cron.db   # relative to data.path
```

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

## Web Browsing

Two complementary tools:

- `web_search` - DuckDuckGo search.
- `open_url` - browser-renders a page and returns elements, text, HTML, or a screenshot.

Browser automation (click, type, extract, screenshot) runs on **Camoufox**, a Firefox build hardened against fingerprinting and bot detection. One Camoufox instance is shared per agent session across all browser tools. By default it runs headful under Xvfb (`headless: "virtual"` in config) rather than plain headless, since plain headless Firefox is easily detected by bot checks; set `headless: true` for genuine headless, or `false` to show a real window. Screenshots are saved under `workspace/outputs/browser/` and returned inline when under `screenshot_max_bytes`.

```yaml
web:
  headless: virtual   # true | false | "virtual"
  timeout_ms: 30000
  screenshot_max_bytes: 1500000
  search_results: 5
```

---

## Tools

| Tool | What it does |
|------|-------------|
| `shell` | Run a shell command; sandboxed by default, or in the main container with `backend_access=True` (requires `backend_exec`) |
| `view` | Read a file with line numbers, or list a directory |
| `write_file` | Create or write to a file (append, prepend, overwrite) |
| `edit_file` | Edit an existing file by replacing a string |
| `grep` | Search file contents with regex (ripgrep with Python fallback) |
| `glob_search` | Find files by name pattern, sorted by modification time |
| `web_search` | Search the web via DuckDuckGo |
| `open_url` | Open any URL in a Camoufox browser; returns elements/text/html/screenshot |
| `memory_search` | Search the RAG semantic index |
| `kg_search` | Search the LadybugDB knowledge graph |
| `kg_traverse` | Traverse graph relationships from a starting entity |
| `call_librarian` | Trigger on-demand knowledge-graph extraction |
| `spawn_agent` | Start a detached subagent on a child branch |
| `wait_agent` | Wait for a spawned subagent to finish or poll its status |
| `add_cron` | Schedule a future agent turn (recurring or one-shot) |
| `list_cron` | List cron jobs in the caller's own channel |
| `remove_cron` | Remove a cron job the caller created (or any job, with `cron_admin`) |
| `use_skill` | Load a skill by name |
| `todo_write` | Update the session task checklist |
| `todo_read` | View the current task list |
| `present` | Deliver files to the user via the active bridge |
| `tools_search` | BM25 + vector search over available tools; enables matching deferred tools |

Write tools (`write_file`, `edit_file`) require the file to have been read first via `view()` - this prevents blind overwrites.

---

## Configuration Reference

Full annotated config: see `example.config.yaml`. Key top-level keys:

| Key | Default | Purpose |
|-----|---------|---------|
| `context` | `16384` | Token budget (recommend `32768`) |
| `max_tool_cycles` | `10` | Max tool-call iterations per turn |
| `workspace.path` | `<instance>/workspace` | Agent-visible working directory |
| `data.path` | `<instance>/data` | Internal state (agent.db, users.db, cron.db, memory graph, cursors) - not visible to the agent's own filesystem tools |
| `filesystem.read_only_paths` | `[]` | Extra directories `view`/`grep`/`glob_search` can see (never write to) - e.g. `/app` |
| `llm.primary` | - | Primary model name (must be `kind: chat`) |
| `llm.fallback` | `[]` | Fallback model names, tried in order |
| `permissions.template` | fully closed | The capability set every user gets by default |
| `permissions.minimal_tokens` | `true` | Hide tools the caller cannot use |
| `web.headless` | `"virtual"` | Camoufox display mode: `true` \| `false` \| `"virtual"` |
| `cron.store_file` | `cron.db` | Cron job store, relative to `data.path` |
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
      prefix_required: true
      command_prefix: "!"
```

Required bot intents: **Message Content**, **Server Members**. Required permissions: Read Messages, Send Messages, Read Message History. Who may DM the bot or `/reset` in a server is governed by the `dm_access` and `manage_ctx` capabilities on the caller, not a separate config threshold.

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

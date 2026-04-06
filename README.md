# TinyCTX

A context-efficent agentic assistant framework inspired by OpenClaw and Nanobot. Connect it to your LLM, configure a bridge (CLI, Discord, Matrix, or HTTP), and you have a persistent, tool-using AI agent.


## Highlights

- Effortless onboarding system
- Optimized for low context - Even 16k is enough
- Active memory consolidation
- Terminal UI with persistent session restore, slash commands, paste refs, and copy helpers
- Direct web browsing via `web_search` and `open_url` (always browser-rendered; headless by default, windowed on request for captchas)
- Proactive heartbeat and cron system
- Static + searchable memory with BM25 or embeddings, plus background memory consolidation
- Subagent support
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
git clone https://github.com/itzpingcat/TinyCTX
cd TinyCTX
pip install -e .
python -m TinyCTX onboard
```

This will start the interactive configuration system.

## Workspace

The workspace is a directory on disk where the agent keeps its persistent state. Default: `~/.tinyctx`. Change it in `config.yaml` or when running onboard:

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
├── downloads/       # Files and images sent by users via bridges; agent can read these
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

## Conversation Consolidation

TinyCTX persists the full conversation tree in `agent.db`, but it does not blindly let old messages drop from context forever. When the active turn gets close to the configured context limit, the agent consolidates important information to memory.

This means:

- older work is still preserved on disk
- the agent can still remember things from a long time ago, even if you only have 16k context
- you dont have to reexplain everything when you have a new session

Cosolidation is automatic; no extra configuration is required beyond setting a sane `context:` limit for your model.

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

They are good for isolated side tasks, not trivial work you can finish faster in the current turn.

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
| `open_url` | Open any URL in a browser and return text, raw HTML, or an interactive element map. Pass `headless=False` to make the window visible (useful for captchas and login walls). |
| `memory_search` | Search the semantic memory index |
| `spawn_agent` | Start a detached subagent on a child branch |
| `wait_agent` | Wait for a spawned subagent to finish or poll its current status |
| `use_skill` | Load a skill by name |
| `todo_write` | Update the session task checklist (for multi-step work) |
| `todo_read` | View the current task list |

Write tools (`write_file`, `str_replace`) require the file to have been read first via `view()` — this prevents blind overwrites and catches stale edits from external changes.

Modules are enabled automatically if their directory exists under `modules/`. No configuration needed beyond having the right dependencies installed.

"""
bridges/cli/__main__.py — Robust Interactive CLI bridge using Rich.
prompt_toolkit removed — no more patch_stdout fighting with Rich Live.
"""
from __future__ import annotations

import asyncio
import re
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pyfiglet
from rich.console import Console, Group
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.live import Live

from contracts import (
    Platform, ContentType,
    UserIdentity, InboundMessage,
    AgentThinkingChunk, AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError,
)

logger = logging.getLogger(__name__)

# --- Code block label injection ---
# Rich's Markdown renderer syntax-highlights fenced blocks but drops the language label.
# We prepend an italic label above each tagged block so the user can see the language.

_FENCED_CODE = re.compile(r'^```[ \t]*(\w+)[ \t]*\n(.*?)\n```[ \t]*$', re.DOTALL | re.MULTILINE)

def _preprocess(text: str) -> str:
    def _label(m: re.Match) -> str:
        lang, body = m.group(1), m.group(2)
        return f'*{lang}*\n```{lang}\n{body}\n```'
    return _FENCED_CODE.sub(_label, text)

# --- Theme & UI ---

@dataclass
class CLITheme:
    colors: dict[str, str] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)

    def c(self, key: str) -> str:
        defaults = {
            "banner": "bright_cyan", "tagline": "bright_black", "border": "bright_black",
            "user_label": "green", "agent_label": "cyan", "thinking": "yellow",
            "tool_call": "bright_black", "tool_ok": "green", "tool_error": "red",
            "reset": "yellow", "error": "red",
        }
        return self.colors.get(key) or defaults.get(key, "")

    def t(self, key: str) -> str:
        defaults = {
            "name": "TinyCTX", "tagline": "Agent Framework",
            "user_label": "you", "agent_label": "agent", "bye_message": "Bye.",
        }
        return self.text.get(key) or defaults.get(key, "")


# --- The Bridge ---

class CLIBridge:
    def __init__(self, gateway, options: dict | None = None) -> None:
        self._gateway = gateway
        self._theme = CLITheme(
            colors=options.get("customcolors") or {} if options else {},
            text=options.get("customtext") or {} if options else {}
        )
        self._console = Console(highlight=False)
        self._reply_done = asyncio.Event()

        self._current_content = ""
        self._live: Live | None = None
        self._cursor: str | None = None  # node_id for this CLI session
        self._label_printed = False

    def _start_reply(self):
        if not self._label_printed:
            t = self._theme.t
            c = self._theme.c
            self._console.print(f"{t('agent_label')}:", style=c('agent_label'))
            self._label_printed = True  # Lock it so it never prints again this turn

    def _get_live_render(self, content: str, is_thinking: bool = False) -> Group:
        """Helper to create a renderable group for the Live display."""
        c = self._theme.c
        parts = []
        if is_thinking and not content:
            parts.append(Text(" ⠋ thinking...", style=c('thinking')))
        
        if content:
            # Preprocess and render the markdown accumulated so far
            parts.append(Markdown(_preprocess(content)))
            
        return Group(*parts)

    def _stop_live(self):
        """Helper to safely stop and clear the live reference."""
        if self._live:
            self._live.stop()
            self._live = None

    def _ensure_live(self, is_thinking: bool = False):
        """Helper to ensure the live display is running."""
        if not self._live:
            self._live = Live(
                self._get_live_render(self._current_content, is_thinking),
                console=self._console,
                refresh_per_second=12,
                vertical_overflow="visible"
            )
            self._live.start()

    async def handle_event(self, event) -> None:
        c = self._theme.c

        if isinstance(event, AgentThinkingChunk):
            self._start_reply()
            self._ensure_live(is_thinking=True)
            # Only update if live is actually active
            if self._live:
                self._live.update(self._get_live_render(self._current_content, is_thinking=True))

        elif isinstance(event, AgentTextChunk):
            self._start_reply()
            self._current_content += event.text
            self._ensure_live()
            if self._live:
                self._live.update(self._get_live_render(self._current_content))

        elif isinstance(event, AgentToolCall):
            # IMPORTANT: Remove 'thinking' from the UI BEFORE stopping
            if self._live:
                self._live.update(self._get_live_render(self._current_content, is_thinking=False))
            self._stop_live()
            self._current_content = ""
            def _truncate(v, max_chars=80) -> str:
                r = repr(v)
                return r[:max_chars] + "..." if len(r) > max_chars else r
            args_str = ", ".join(f"{k}={_truncate(v)}" for k, v in event.args.items())
            self._console.print(f"  [{c('tool_call')}]⟶  {event.tool_name}({args_str})[/{c('tool_call')}]")

        elif isinstance(event, AgentToolResult):
            self._stop_live()
            status_color = c("tool_error") if event.is_error else c("tool_ok")
            icon = "✗" if event.is_error else "✓"
            preview = event.output[:100].replace("\n", " ") + ("..." if len(event.output) > 100 else "")
            self._console.print(f"  [{status_color}]{icon}  {event.tool_name}:[/{status_color}] ", end="")
            self._console.print(preview, markup=False, style="bright_black")

        elif isinstance(event, AgentTextFinal):
            final_text = (event.text or self._current_content).strip()
            # If we were streaming, update one last time then stop
            if self._live:
                self._live.update(self._get_live_render(final_text))
            
            self._stop_live()
            
            # RESET EVERYTHING
            self._current_content = ""
            self._label_printed = False 
            self._reply_done.set()

        elif isinstance(event, AgentError):
            self._stop_live()
            self._console.print(f"\n[{c('error')}]error: {event.message}[/{c('error')}]\n")
            self._reply_done.set()

    async def _prompt(self, prompt_str: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: input(prompt_str))

    async def run(self) -> None:
        # Route all logging through Rich so log lines don't interleave with
        # the input() prompt or Live panels (fixes heartbeat log bleed).
        config = getattr(self._gateway, "_config", None)
        log_level = logging.WARNING
        if config and hasattr(config, "logging"):
            level_str = getattr(config.logging, "level", "WARNING")
            log_level = getattr(logging, level_str.upper(), logging.WARNING)

        logging.basicConfig(
            level=log_level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(console=self._console, rich_tracebacks=True, markup=False)],
            force=True,
        )
        # markdown-it-py logs every parser rule at DEBUG; silence it regardless
        # of the configured log level — it's never useful in the CLI output.
        logging.getLogger("markdown_it").setLevel(logging.WARNING)
        self._gateway.register_platform_handler(Platform.CLI.value, self.handle_event)
        self._cursor = _load_cli_cursor(self._gateway)

        banner_text = Text()
        banner_text.append(pyfiglet.figlet_format(self._theme.t("name"), font="slant"), style=self._theme.c("banner"))
        banner_text.append(f"  {self._theme.t('tagline')}", style=self._theme.c("tagline"))
        self._console.print(Panel(banner_text, border_style=self._theme.c("border"), padding=(0, 2)))
        self._console.print(f"[{self._theme.c('border')}]  type a message · /reset · /debug heartbeat · exit[/{self._theme.c('border')}]\n")

        c = self._theme.c
        t = self._theme.t

        ANSI_RESET = "\033[0m"
        ANSI_GREEN = "\033[32m"
        prompt_str = f"{ANSI_GREEN}{t('user_label')}{ANSI_RESET}: "

        while True:
            try:
                text = await self._prompt(prompt_str)
                text = text.strip()
                if not text:
                    continue
                if text.lower() in {"exit", "quit"}:
                    break

                if text.startswith("/"):
                    if text.lower() == "/debug heartbeat":
                        await _debug_heartbeat(self._gateway, self._console, c)
                        continue
                    elif text.lower() == "/reset":
                        # Start a brand new session branch off root.
                        from db import ConversationDB
                        workspace   = Path(self._gateway._config.workspace.path).expanduser().resolve()
                        db          = ConversationDB(workspace / "agent.db")
                        root        = db.get_root()
                        node        = db.add_node(parent_id=root.id, role="system", content="session:cli")
                        cursor_file = workspace / "cursors" / "cli"
                        cursor_file.write_text(node.id, encoding="utf-8")
                        self._cursor = node.id
                        self._gateway.reset_lane(self._cursor)
                        self._console.print(f"[{c('reset')}]  ↺  new session started[/{c('reset')}]")
                    elif text.lower() == "/resume":
                        # Already on the latest session — just confirm.
                        self._console.print(f"[{c('reset')}]  ↩  resuming from last session[/{c('reset')}]")
                    continue

                msg = InboundMessage(
                    tail_node_id=self._cursor,
                    author=CLI_USER,
                    content_type=ContentType.TEXT,
                    text=text,
                    message_id=str(time.time_ns()),
                    timestamp=time.time(),
                )
                self._reply_done.clear()
                await self._gateway.push(msg)
                await self._reply_done.wait()
                _persist_cli_cursor(self._gateway, self._cursor)

            except (KeyboardInterrupt, EOFError):
                break

        self._console.print(f"[{c('reset')}]{t('bye_message')}[/{c('reset')}]")


# --- Debug commands ---

async def _debug_heartbeat(gateway, console: Console, c) -> None:
    """
    /debug heartbeat — immediately fire one heartbeat tick so you can verify
    the module is wired correctly without waiting for the real interval.

    Looks for a running heartbeat task on the event loop by name
    ("heartbeat:<node_id>") and calls _tick() directly on the same lane/tail
    the live task uses, so the test exercises the real code path.
    """
    from modules.heartbeat.__main__ import _tick, _parse_reply

    # Find the heartbeat task and extract its lane/tail state from the agent.
    # The task name is "heartbeat:<lane_node_id>" — see register() in heartbeat.
    hb_task = next(
        (t for t in asyncio.all_tasks() if t.get_name().startswith("heartbeat:")),
        None,
    )

    # heartbeat.register() stashes the AgentLoop ref directly on the gateway.
    agent = getattr(gateway, "_heartbeat_agent", None)

    if agent is None:
        # Fallback: scan live lanes (works after first message if stash missed).
        for lane in gateway._lane_router._lanes.values():
            loop = getattr(lane, "loop", None)
            if loop is None and hasattr(lane, "_lane"):
                loop = getattr(lane._lane, "loop", None)
            if loop is not None and getattr(loop, "_heartbeat_lane_node_id", None):
                agent = loop
                break

    if agent is None:
        console.print(f"[{c('error')}]  ✗  heartbeat: could not find agent instance[/{c('error')}]")
        console.print(f"[{c('tool_call')}]     (is the heartbeat module registered?)[/{c('tool_call')}]")
        return

    lane_node_id = getattr(agent, "_heartbeat_lane_node_id", None)
    tail_node_id = getattr(agent, "_heartbeat_cursor_node_id", None)

    if not lane_node_id:
        console.print(f"[{c('error')}]  ✗  heartbeat: no cursor found — has the module run at least once?[/{c('error')}]")
        if hb_task:
            console.print(f"[{c('tool_call')}]     task exists ({hb_task.get_name()}) but hasn't ticked yet[/{c('tool_call')}]")
        return

    try:
        from modules.heartbeat import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    prompt              = cfg.get("prompt", "If nothing needs attention, reply HEARTBEAT_OK.")
    continuation_prompt = cfg.get("continuation_prompt", "Continue the task, or reply HEARTBEAT_OK when you are done.")
    ack_max             = int(cfg.get("ack_max_chars", 300))
    max_continuations   = int(cfg.get("max_continuations", 5))

    console.print(f"[{c('tool_call')}]  ⏱  firing heartbeat tick (lane={lane_node_id[:8]}…)[/{c('tool_call')}]")

    try:
        new_tail = await _tick(
            agent, lane_node_id, tail_node_id,
            prompt, continuation_prompt,
            ack_max, max_continuations,
        )
        # Update the persisted tail so the real task picks up from the right node.
        setattr(agent, "_heartbeat_cursor_node_id", new_tail)
        console.print(f"[{c('tool_ok')}]  ✓  heartbeat tick complete[/{c('tool_ok')}]")
    except Exception as exc:
        console.print(f"[{c('error')}]  ✗  heartbeat tick raised: {exc}[/{c('error')}]")


# CLI user identity
CLI_USER_ID = "cli-owner"
CLI_USER = UserIdentity(platform=Platform.CLI, user_id=CLI_USER_ID, username="you")


def _load_cli_cursor(gateway) -> str:
    """
    Load (or create) the persistent CLI cursor from workspace/cursors/cli.
    On first run, attaches to the DB global root and persists the new node_id.
    On subsequent runs, resumes from the last known tail node.
    """
    from db import ConversationDB
    workspace   = Path(gateway._config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    cursors_dir = workspace / "cursors"
    cursors_dir.mkdir(parents=True, exist_ok=True)
    cursor_file = cursors_dir / "cli"
    db_path     = workspace / "agent.db"
    db          = ConversationDB(db_path)

    if cursor_file.exists():
        node_id = cursor_file.read_text(encoding="utf-8").strip()
        if db.get_node(node_id) is not None:
            return node_id

    root    = db.get_root()
    node    = db.add_node(parent_id=root.id, role="system", content="session:cli")
    cursor_file.write_text(node.id, encoding="utf-8")
    return node.id


def _persist_cli_cursor(gateway, anchor_node_id: str) -> None:
    """
    After each turn, read the live tail from the active lane and persist it
    to the cursor file so the next session resumes from the right place.
    """
    try:
        lanes = gateway._lane_router._lanes
        # The lane may be keyed by the anchor or by the current tail.
        lane = lanes.get(anchor_node_id)
        if lane is None:
            # Search all lanes for one whose original node_id matches.
            lane = next((l for l in lanes.values() if l.node_id == anchor_node_id), None)
        if lane is None:
            return
        tail = lane.loop._tail_node_id
        if not tail:
            return
        workspace   = Path(gateway._config.workspace.path).expanduser().resolve()
        cursor_file = workspace / "cursors" / "cli"
        cursor_file.write_text(tail, encoding="utf-8")
    except Exception:
        pass  # cursor persistence must never crash the bridge



# --- Loader entry point ---

async def run(gateway) -> None:
    """Entry point called by main.py loader."""
    options = {}

    config = getattr(gateway, "_config", None)
    if config and hasattr(config, "bridges"):
        bridge_cfg = config.bridges.get("cli")
        if bridge_cfg:
            options = getattr(bridge_cfg, "options", {})

    bridge = CLIBridge(gateway, options=options)
    await bridge.run()


if __name__ == "__main__":
    from config import load as load_config, apply_logging
    from gateway import Gateway
    async def _main():
        cfg = load_config()
        apply_logging(cfg.logging)
        await run(Gateway(config=cfg))
    asyncio.run(_main())
"""
bridges/cli/__main__.py — Robust Interactive CLI bridge using Rich.
prompt_toolkit removed — no more patch_stdout fighting with Rich Live.

Slash commands are dispatched via router.commands (CommandRegistry).
Built-in bridge commands (/reset, /resume, /copy, /paste, /help) are
handled here before the registry is consulted.
"""
from __future__ import annotations

import asyncio
import re
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

import sys
import pyfiglet

if sys.platform == "win32":
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

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
        self._last_reply: str = ""

    # --- Clipboard helpers ---

    def _read_clipboard_text(self) -> str:
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, timeout=2, encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                return result.stdout
        except Exception:
            pass
        return ""

    def _write_clipboard_text(self, text: str) -> bool:
        if not text:
            return False
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=text, capture_output=True, text=True, timeout=2,
                encoding="utf-8", errors="replace",
            )
            return result.returncode == 0
        except Exception:
            return False

    # --- Reply display helpers ---

    def _start_reply(self):
        if not self._label_printed:
            t = self._theme.t
            c = self._theme.c
            self._console.print(f"{t('agent_label')}:", style=c('agent_label'))
            self._label_printed = True

    def _get_live_render(self, content: str, is_thinking: bool = False) -> Group:
        c = self._theme.c
        parts = []
        if is_thinking and not content:
            parts.append(Text(" ⠋ thinking...", style=c('thinking')))
        if content:
            parts.append(Markdown(_preprocess(content)))
        return Group(*parts)

    def _stop_live(self):
        if self._live:
            self._live.stop()
            self._live = None

    def _ensure_live(self, is_thinking: bool = False):
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
            if self._live:
                self._live.update(self._get_live_render(self._current_content, is_thinking=True))

        elif isinstance(event, AgentTextChunk):
            self._start_reply()
            self._current_content += event.text
            self._ensure_live()
            if self._live:
                self._live.update(self._get_live_render(self._current_content))

        elif isinstance(event, AgentToolCall):
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
            if self._live:
                self._live.update(self._get_live_render(final_text))
            self._stop_live()
            if final_text:
                self._last_reply = final_text
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

    def _build_command_context(self) -> dict:
        """Build the context dict passed to module command handlers."""
        return {
            "console":  self._console,
            "theme_c":  self._theme.c,
            "gateway":  self._gateway,
            "cursor":   self._cursor,
        }

    async def _handle_help(self) -> None:
        """Print all registered commands plus built-in bridge commands."""
        c = self._theme.c
        rows = [
            ("/reset",   "Start a new session branch"),
            ("/resume",  "Confirm resume from last session"),
            ("/copy",    "Copy last agent reply to clipboard"),
            ("/paste",   "Submit clipboard contents as next message"),
            ("/help",    "Show this help"),
        ]
        # Append module-registered commands.
        registry = getattr(self._gateway, "commands", None)
        if registry is not None:
            rows.extend(registry.list_commands())
        rows.sort(key=lambda r: r[0])
        self._console.print(f"[{c('border')}]available commands:[/{c('border')}]")
        for cmd, help_text in rows:
            self._console.print(f"  [{c('tool_call')}]{cmd}[/{c('tool_call')}]  {help_text}")

    async def run(self) -> None:
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
        logging.getLogger("markdown_it").setLevel(logging.WARNING)
        self._gateway.register_platform_handler(Platform.CLI.value, self.handle_event)
        self._cursor = _load_cli_cursor(self._gateway)

        # Eagerly open the lane so modules load and register their commands
        # into router.commands before the user types anything.
        self._gateway.open_lane(self._cursor, Platform.CLI.value)

        banner_text = Text()
        banner_text.append(pyfiglet.figlet_format(self._theme.t("name"), font="slant"), style=self._theme.c("banner"))
        banner_text.append(f"  {self._theme.t('tagline')}", style=self._theme.c("tagline"))
        self._console.print(Panel(banner_text, border_style=self._theme.c("border"), padding=(0, 2)))
        self._console.print(f"[{self._theme.c('border')}]  type a message · /reset · /help · exit[/{self._theme.c('border')}]\n")

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
                    # Built-in bridge commands (not module-registered).
                    lower = text.lower()

                    if lower == "/reset":
                        from db import ConversationDB
                        workspace   = Path(self._gateway._config.workspace.path).expanduser().resolve()
                        db          = ConversationDB(workspace / "agent.db")
                        root        = db.get_root()
                        node        = db.add_node(parent_id=root.id, role="system", content="session:cli")
                        cursor_file = workspace / "cursors" / "cli"
                        cursor_file.write_text(node.id, encoding="utf-8")
                        self._cursor = node.id
                        self._gateway.reset_lane(self._cursor)
                        # Eagerly open the new lane so commands remain available.
                        self._gateway.open_lane(self._cursor, Platform.CLI.value)
                        self._console.print(f"[{c('reset')}]  ↺  new session started[/{c('reset')}]")
                        continue

                    if lower == "/resume":
                        self._console.print(f"[{c('reset')}]  ↩  resuming from last session[/{c('reset')}]")
                        continue

                    if lower.startswith("/copy"):
                        copied = self._write_clipboard_text(self._last_reply)
                        if copied:
                            self._console.print(f"[{c('tool_ok')}]  ✓  copied last reply to clipboard[/{c('tool_ok')}]")
                        else:
                            self._console.print(f"[{c('tool_error')}]  ✗  nothing to copy[/{c('tool_error')}]")
                        continue

                    if lower.startswith("/paste"):
                        pasted = self._read_clipboard_text().strip()
                        if not pasted:
                            self._console.print(f"[{c('tool_error')}]  ✗  clipboard is empty[/{c('tool_error')}]")
                            continue
                        self._console.print(f"[{c('tool_call')}]  (pasting {len(pasted)} chars from clipboard)[/{c('tool_call')}]")
                        text = pasted
                        # Falls through to the InboundMessage send below.

                    elif lower in {"/help", "/?"}:
                        await self._handle_help()
                        continue

                    else:
                        # Delegate to the module command registry.
                        registry = getattr(self._gateway, "commands", None)
                        if registry is not None:
                            handled = await registry.dispatch(text, self._build_command_context())
                            if handled:
                                continue
                        # Unrecognised slash command — suggest /help.
                        self._console.print(
                            f"[{c('error')}]  unknown command: {text}  (try /help)[/{c('error')}]"
                        )
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


# CLI user identity
CLI_USER_ID = "cli-owner"
CLI_USER = UserIdentity(platform=Platform.CLI, user_id=CLI_USER_ID, username="you")


def _load_cli_cursor(gateway) -> str:
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
    try:
        lanes = gateway._lane_router._lanes
        lane = lanes.get(anchor_node_id)
        if lane is None:
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

"""
bridges/cli/__main__.py — Interactive CLI bridge (detached / HTTP mode).

In the new architecture the CLI bridge does NOT run inside the daemon. It
attaches to a running TinyCTX daemon over HTTP/SSE on demand via
`tinyctx launch cli`.

MANUAL_LAUNCH = True tells main.py's auto-start loop to skip this bridge.

Entry points
------------
run(gateway)              No-op. Called by main.py before the MANUAL_LAUNCH
                          check fires — never actually reached.
run_detached(...)         Real entry point. Called by cmd/commands/launch.py.
                          Connects to the daemon over HTTP and runs the TUI.

What is unchanged
-----------------
  CLITheme, CLIBridge.__init__, _console, _live, _theme, _reply_done
  handle_event, _start_reply, _get_live_render, _stop_live, _ensure_live
  _preprocess (code block label injection)
  _read_clipboard_text, _write_clipboard_text
  /copy, /paste, /help built-in slash commands
  _prompt (async stdin reader)
  _load_cli_cursor_path / _save_cli_cursor_path (cursor file helpers)

What changed
------------
  CLIBridge gains _gateway_url and _api_key (set by run_detached).
  _send() POSTs to /v1/lane/message and reads SSE instead of calling router.
  /reset calls POST /v1/lane/branch then POST /v1/lane/open.
  Module slash commands removed (no in-process router).
"""
from __future__ import annotations

import asyncio
import json
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

logger = logging.getLogger(__name__)

# Sentinel: main.py skips bridges that set this to True.
MANUAL_LAUNCH = True

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


# --- Fake event objects for handle_event ---
# The renderer reads only: .text, .tool_name, .args, .call_id,
#                          .output, .is_error, .message
# We don't need real contract dataclasses for the HTTP path.

class _FakeThinkingChunk:
    def __init__(self, text): self.text = text
class _FakeTextChunk:
    def __init__(self, text): self.text = text
class _FakeTextFinal:
    def __init__(self, text): self.text = text
class _FakeToolCall:
    def __init__(self, tool_name, call_id, args):
        self.tool_name = tool_name; self.call_id = call_id; self.args = args
class _FakeToolResult:
    def __init__(self, tool_name, call_id, output, is_error):
        self.tool_name = tool_name; self.call_id = call_id
        self.output = output; self.is_error = is_error
class _FakeError:
    def __init__(self, message): self.message = message

# Import the real contract types only for isinstance checks in handle_event.
from contracts import (
    AgentThinkingChunk, AgentTextChunk, AgentTextFinal,
    AgentToolCall, AgentToolResult, AgentError,
)

# --- The Bridge ---

class CLIBridge:
    def __init__(self, gateway, options: dict | None = None) -> None:
        # gateway is None in detached mode; kept for signature compat.
        self._gateway     = gateway
        self._theme       = CLITheme(
            colors=options.get("customcolors") or {} if options else {},
            text=options.get("customtext") or {} if options else {}
        )
        self._console     = Console(highlight=False)
        self._reply_done  = asyncio.Event()

        self._current_content = ""
        self._live: Live | None = None
        self._cursor: str | None = None   # node_id cursor, managed locally
        self._label_printed   = False
        self._last_reply: str = ""

        # Set by run_detached before run() is called.
        self._gateway_url: str = ""
        self._api_key: str     = ""

    # --- HTTP helpers ---

    def _http_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
        }

    async def _api_post(self, path: str, payload: dict) -> dict:
        """POST JSON to the gateway, return parsed response dict."""
        import aiohttp
        url = f"{self._gateway_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    headers=self._http_headers()) as resp:
                resp.raise_for_status()
                return await resp.json()

    # --- Clipboard helpers ---

    def _read_clipboard_text(self) -> str:
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-Clipboard -Raw"],
                capture_output=True, text=True, timeout=2,
                encoding="utf-8", errors="replace",
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

        if isinstance(event, (AgentThinkingChunk, _FakeThinkingChunk)):
            self._start_reply()
            self._ensure_live(is_thinking=True)
            if self._live:
                self._live.update(self._get_live_render(
                    self._current_content, is_thinking=True))

        elif isinstance(event, (AgentTextChunk, _FakeTextChunk)):
            self._start_reply()
            self._current_content += event.text
            self._ensure_live()
            if self._live:
                self._live.update(self._get_live_render(self._current_content))

        elif isinstance(event, (AgentToolCall, _FakeToolCall)):
            if self._live:
                self._live.update(self._get_live_render(
                    self._current_content, is_thinking=False))
            self._stop_live()
            self._current_content = ""
            def _truncate(v, max_chars=80) -> str:
                r = repr(v)
                return r[:max_chars] + "..." if len(r) > max_chars else r
            args_str = ", ".join(
                f"{k}={_truncate(v)}" for k, v in event.args.items())
            self._console.print(
                f"  [{c('tool_call')}]⟶  {event.tool_name}({args_str})[/{c('tool_call')}]")

        elif isinstance(event, (AgentToolResult, _FakeToolResult)):
            self._stop_live()
            status_color = c("tool_error") if event.is_error else c("tool_ok")
            icon = "✗" if event.is_error else "✓"
            preview = (event.output[:100].replace("\n", " ")
                       + ("..." if len(event.output) > 100 else ""))
            self._console.print(
                f"  [{status_color}]{icon}  {event.tool_name}:[/{status_color}] ",
                end="")
            self._console.print(preview, markup=False, style="bright_black")

        elif isinstance(event, (AgentTextFinal, _FakeTextFinal)):
            final_text = (event.text or self._current_content).strip()
            if self._live:
                self._live.update(self._get_live_render(final_text))
            self._stop_live()
            if final_text:
                self._last_reply = final_text
            self._current_content = ""
            self._label_printed   = False
            self._reply_done.set()

        elif isinstance(event, (AgentError, _FakeError)):
            self._stop_live()
            self._console.print(
                f"\n[{c('error')}]error: {event.message}[/{c('error')}]\n")
            self._reply_done.set()

    # --- HTTP send (replaces router.push) ---

    async def _send(self, text: str, attachments=()) -> None:
        """
        POST a message to /v1/lane/message, consume the SSE stream,
        feed each event into handle_event, and advance the local cursor
        on the done event.
        """
        import aiohttp

        payload: dict = {"node_id": self._cursor, "text": text}
        if attachments:
            payload["attachments"] = attachments

        url = f"{self._gateway_url}/v1/lane/message"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=self._http_headers()
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = data.get("type")

                    if event_type == "thinking_chunk":
                        await self.handle_event(_FakeThinkingChunk(data.get("text", "")))
                    elif event_type == "text_chunk":
                        await self.handle_event(_FakeTextChunk(data.get("text", "")))
                    elif event_type == "text_final":
                        await self.handle_event(_FakeTextFinal(data.get("text", "")))
                    elif event_type == "tool_call":
                        await self.handle_event(_FakeToolCall(
                            data.get("tool_name", ""),
                            data.get("call_id", ""),
                            data.get("args", {}),
                        ))
                    elif event_type == "tool_result":
                        await self.handle_event(_FakeToolResult(
                            data.get("tool_name", ""),
                            data.get("call_id", ""),
                            data.get("output", ""),
                            bool(data.get("is_error", False)),
                        ))
                    elif event_type == "error":
                        await self.handle_event(_FakeError(data.get("message", "")))
                    elif event_type == "done":
                        new_tail = data.get("node_id")
                        if new_tail:
                            self._cursor = new_tail
                            _save_cli_cursor_path(new_tail)
                        break

    async def _prompt(self, prompt_str: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: input(prompt_str))

    async def _handle_help(self) -> None:
        c = self._theme.c
        rows = [
            ("/reset",  "Start a new session branch"),
            ("/copy",   "Copy last agent reply to clipboard"),
            ("/paste",  "Submit clipboard contents as next message"),
            ("/help",   "Show this help"),
        ]
        rows.sort(key=lambda r: r[0])
        self._console.print(
            f"[{c('border')}]available commands:[/{c('border')}]")
        for cmd, help_text in rows:
            self._console.print(
                f"  [{c('tool_call')}]{cmd}[/{c('tool_call')}]  {help_text}")

    async def run(self) -> None:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(console=self._console,
                                  rich_tracebacks=True, markup=False)],
            force=True,
        )
        logging.getLogger("markdown_it").setLevel(logging.WARNING)

        banner_text = Text()
        banner_text.append(
            pyfiglet.figlet_format(self._theme.t("name"), font="slant"),
            style=self._theme.c("banner"),
        )
        banner_text.append(
            f"  {self._theme.t('tagline')}", style=self._theme.c("tagline"))
        self._console.print(Panel(
            banner_text,
            border_style=self._theme.c("border"),
            padding=(0, 2),
        ))
        self._console.print(
            f"[{self._theme.c('border')}]"
            "  type a message · /reset · /help · exit"
            f"[/{self._theme.c('border')}]\n"
        )

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
                    lower = text.lower()

                    if lower == "/reset":
                        # Branch off root (non-destructive), then open lane.
                        branch_data = await self._api_post(
                            "/v1/lane/branch", {"parent_node_id": None})
                        new_node_id = branch_data["node_id"]
                        await self._api_post(
                            "/v1/lane/open", {"node_id": new_node_id})
                        self._cursor = new_node_id
                        _save_cli_cursor_path(new_node_id)
                        self._console.print(
                            f"[{c('reset')}]  ↺  new session started"
                            f"[/{c('reset')}]")
                        continue

                    if lower.startswith("/copy"):
                        copied = self._write_clipboard_text(self._last_reply)
                        if copied:
                            self._console.print(
                                f"[{c('tool_ok')}]  ✓  copied last reply to clipboard"
                                f"[/{c('tool_ok')}]")
                        else:
                            self._console.print(
                                f"[{c('tool_error')}]  ✗  nothing to copy"
                                f"[/{c('tool_error')}]")
                        continue

                    if lower.startswith("/paste"):
                        pasted = self._read_clipboard_text().strip()
                        if not pasted:
                            self._console.print(
                                f"[{c('tool_error')}]  ✗  clipboard is empty"
                                f"[/{c('tool_error')}]")
                            continue
                        self._console.print(
                            f"[{c('tool_call')}]  (pasting {len(pasted)} chars "
                            f"from clipboard)[/{c('tool_call')}]")
                        text = pasted
                        # Falls through to _send below.

                    elif lower in {"/help", "/?"}:
                        await self._handle_help()
                        continue

                    else:
                        self._console.print(
                            f"[{c('error')}]  unknown command: {text}"
                            f"  (try /help)[/{c('error')}]")
                        continue

                self._reply_done.clear()
                await self._send(text)
                # _send() drives the full SSE loop and sets _reply_done
                # indirectly via handle_event on AgentTextFinal/AgentError.

            except (KeyboardInterrupt, EOFError):
                break

        self._console.print(f"[{c('reset')}]{t('bye_message')}[/{c('reset')}]")


# ---------------------------------------------------------------------------
# Cursor file helpers (client-side, ~/.tinyctx/cursors/cli)
# ---------------------------------------------------------------------------

_CURSOR_FILE = Path.home() / ".tinyctx" / "cursors" / "cli"


def _load_cli_cursor_path() -> str | None:
    try:
        if _CURSOR_FILE.exists():
            return _CURSOR_FILE.read_text(encoding="utf-8").strip() or None
    except Exception:
        pass
    return None


def _save_cli_cursor_path(node_id: str) -> None:
    try:
        _CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CURSOR_FILE.write_text(node_id, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def run(gateway) -> None:
    """
    Called by main.py bridge loader. Skipped before reaching here because
    MANUAL_LAUNCH = True, but kept as a no-op for safety.
    """
    pass


async def run_detached(
    gateway_url: str,
    api_key: str,
    options: dict | None = None,
) -> None:
    """
    Real entry point — called by cmd/commands/launch.py.
    Connects to the running daemon over HTTP and runs the interactive TUI.
    """
    import aiohttp

    bridge = CLIBridge(None, options=options or {})
    bridge._gateway_url = gateway_url
    bridge._api_key     = api_key

    # Resolve or create the cursor via /v1/lane/open.
    saved_cursor = _load_cli_cursor_path()
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{gateway_url}/v1/lane/open",
            json={"node_id": saved_cursor},
            headers=bridge._http_headers(),
        )
        resp.raise_for_status()
        data = await resp.json()

    bridge._cursor = data["node_id"]
    _save_cli_cursor_path(bridge._cursor)

    await bridge.run()

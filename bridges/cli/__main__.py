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

import pyfiglet
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.live import Live

from contracts import (
    Platform, ContentType,
    SessionKey, UserIdentity, InboundMessage,
    AgentThinkingChunk, AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError,
)

logger = logging.getLogger(__name__)
logging.getLogger("markdown_it").setLevel(logging.WARNING)

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

    def _get_live_render(self, content: str, is_thinking: bool = False) -> Group:
        """Helper to create a renderable group for the Live display."""
        c = self._theme.c
        t = self._theme.t
        
        parts = []
        # Add the agent label header
        parts.append(Text(f"{t('agent_label')}:", style=c('agent_label')))
        
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
        t = self._theme.t

        if isinstance(event, AgentThinkingChunk):
            self._ensure_live(is_thinking=True)
            self._live.update(self._get_live_render(self._current_content, is_thinking=True))

        elif isinstance(event, AgentTextChunk):
            self._current_content += event.text
            self._ensure_live()
            self._live.update(self._get_live_render(self._current_content))

        elif isinstance(event, AgentTextFinal):
            # Finalize the current stream
            final_text = (self._current_content + (event.text or "")).strip()
            self._current_content = final_text
            
            if self._live:
                self._live.update(self._get_live_render(self._current_content))
            
            self._stop_live()
            self._console.print() # Final spacer
            
            # Reset state for next turn
            self._current_content = ""
            self._reply_done.set()

        elif isinstance(event, AgentToolCall):
            # 1. Stop streaming so the tool call prints below the text
            self._stop_live()
            
            args_str = ", ".join(f"{k}={v!r}" for k, v in event.args.items())
            self._console.print(f"  [{c('tool_call')}]⟶  {event.tool_name}({args_str})[/{c('tool_call')}]")

        elif isinstance(event, AgentToolResult):
            # 2. Results also print outside the live context
            self._stop_live()
            
            status_color = c("tool_error") if event.is_error else c("tool_ok")
            icon = "✗" if event.is_error else "✓"
            preview = event.output[:100].replace("\n", " ") + ("..." if len(event.output) > 100 else "")
            self._console.print(f"  [{status_color}]{icon}  {event.tool_name}:[/{status_color}] ", end="")
            self._console.print(preview, markup=False, style="bright_black")

        elif isinstance(event, AgentError):
            self._stop_live()
            self._console.print(f"\n[{c('error')}]error: {event.message}[/{c('error')}]\n")
            self._reply_done.set()

    async def _prompt(self, prompt_str: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: input(prompt_str))

    async def run(self) -> None:
        self._gateway.register_platform_handler(Platform.CLI.value, self.handle_event)

        banner_text = Text()
        banner_text.append(pyfiglet.figlet_format(self._theme.t("name"), font="slant"), style=self._theme.c("banner"))
        banner_text.append(f"  {self._theme.t('tagline')}", style=self._theme.c("tagline"))
        self._console.print(Panel(banner_text, border_style=self._theme.c("border"), padding=(0, 2)))
        self._console.print(f"[{self._theme.c('border')}]  type a message · /reset · /next · exit[/{self._theme.c('border')}]\n")

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
                    if text.lower() == "/reset":
                        self._gateway.reset_session(CLI_SESSION)
                        self._console.print(f"[{c('reset')}]  ↺  context cleared[/{c('reset')}]")
                    elif text.lower() == "/next":
                        self._gateway.next_session(CLI_SESSION)
                        self._console.print(f"[{c('reset')}]  ↷  new session[/{c('reset')}]")
                    continue

                msg = InboundMessage(
                    session_key=CLI_SESSION, author=CLI_USER,
                    content_type=ContentType.TEXT, text=text,
                    message_id=str(time.time_ns()), timestamp=time.time(),
                )
                self._reply_done.clear()
                await self._gateway.push(msg)
                await self._reply_done.wait()

            except (KeyboardInterrupt, EOFError):
                break

        self._console.print(f"[{c('reset')}]{t('bye_message')}[/{c('reset')}]")


# Boilerplate Constants
CLI_USER_ID = "cli-owner"
CLI_USER = UserIdentity(platform=Platform.CLI, user_id=CLI_USER_ID, username="you")
CLI_SESSION = SessionKey.dm(CLI_USER_ID)


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
"""
bridges/cli/__main__.py — Interactive CLI bridge.

Exposes run(gateway) for main.py loader.
Can still be run standalone: python -m bridges.cli
"""
from __future__ import annotations

import asyncio
import time
import logging
from typing import NoReturn

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from contracts import (
    Platform, ContentType,
    SessionKey, UserIdentity, InboundMessage, OutboundReply,
)

logger = logging.getLogger(__name__)

CLI_USER_ID = "cli-owner"
CLI_USER    = UserIdentity(platform=Platform.CLI, user_id=CLI_USER_ID, username="you")
CLI_SESSION = SessionKey.dm(CLI_USER_ID)


class CLIBridge:
    def __init__(self, gateway) -> None:
        self._gateway   = gateway
        self._prompt    = PromptSession()
        self._reply_buf: list[str] = []

    async def handle_reply(self, reply: OutboundReply) -> None:
        self._reply_buf.append(reply.text)
        if not reply.is_partial:
            full = "".join(self._reply_buf)
            self._reply_buf.clear()
            print(f"\nagent: {full}\n")

    async def run(self) -> None:
        self._gateway.register_reply_handler(Platform.CLI.value, self.handle_reply)
        print("CLI bridge ready. Type a message, Ctrl-C or 'exit' to quit.\n")

        with patch_stdout():
            while True:
                try:
                    text = await self._prompt.prompt_async("you: ")
                except (KeyboardInterrupt, EOFError):
                    print("\nBye.")
                    break

                text = text.strip()
                if not text:
                    continue
                if text.lower() in {"exit", "quit"}:
                    print("Bye.")
                    break
                if text.lower() == "/reset":
                    self._gateway.reset_session(CLI_SESSION)
                    self._reply_buf.clear()
                    print("\n[context cleared]\n")
                    continue

                msg = InboundMessage(
                    session_key=CLI_SESSION,
                    author=CLI_USER,
                    content_type=ContentType.TEXT,
                    text=text,
                    message_id=str(time.time_ns()),
                    timestamp=time.time(),
                )
                await self._gateway.push(msg)


async def run(gateway) -> None:
    """Entry point called by main.py loader."""
    bridge = CLIBridge(gateway)
    await bridge.run()


if __name__ == "__main__":
    # Standalone mode
    import asyncio
    from config import load as load_config, apply_logging
    from gateway import Gateway

    async def _standalone():
        cfg = load_config()
        apply_logging(cfg.logging)
        await run(Gateway(config=cfg))

    asyncio.run(_standalone())
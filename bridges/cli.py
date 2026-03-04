"""
bridges/cli.py — Interactive CLI bridge using prompt_toolkit.
Connects to the gateway as any other bridge would.

DM session: always uses SessionKey.dm(CLI_USER_ID) — it's always you.
Buffers streamed reply chunks internally, prints on final chunk.

Run directly:  python -m bridges.cli
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
from config import load as load_config, apply_logging
from gateway import Gateway

logger = logging.getLogger(__name__)

# Stable CLI identity — it's always the local user
CLI_USER_ID = "cli-owner"
CLI_USER    = UserIdentity(platform=Platform.CLI, user_id=CLI_USER_ID, username="you")
CLI_SESSION = SessionKey.dm(CLI_USER_ID)


class CLIBridge:
    """
    Reads input from the terminal, pushes InboundMessages to the gateway,
    and prints OutboundReply chunks when the final chunk arrives.
    """

    def __init__(self, gateway: Gateway) -> None:
        self._gateway   = gateway
        self._prompt    = PromptSession()
        self._reply_buf: list[str] = []

    async def handle_reply(self, reply: OutboundReply) -> None:
        """Registered with gateway. Buffers partials, prints on final chunk."""
        self._reply_buf.append(reply.text)
        if not reply.is_partial:
            full = "".join(self._reply_buf)
            self._reply_buf.clear()
            print(f"\nagent: {full}\n")

    async def run(self) -> NoReturn:
        self._gateway.register_reply_handler(Platform.CLI.value, self.handle_reply)
        print("CLI bridge ready. Type a message, Ctrl-C or 'exit' to quit.\n")

        # patch_stdout keeps prompt_toolkit's input line intact
        # when handle_reply calls print() from an async task
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

        await self._gateway.shutdown()


async def main() -> None:
    cfg = load_config()
    apply_logging(cfg.logging)
    gw     = Gateway(config=cfg)
    bridge = CLIBridge(gateway=gw)
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
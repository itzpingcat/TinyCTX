"""
bridges/discord/__main__.py — Discord bridge for TinyCTX.

Uses discord.py (pip install discord.py).

Config (in config.yaml under bridges.discord.options):
  token_env:        Name of the env var holding the bot token.
                    Default: DISCORD_BOT_TOKEN
  allowed_users:    Allowlist of Discord user IDs (integers) permitted to
                    interact with the bot. Empty list = open to everyone.
                    Messages from any user not on this list are silently
                    ignored before being pushed to the router.
                    Default: []  (WARNING: open access — set this!)
  dm_enabled:       Allow DMs to the bot. Default: true
  guild_ids:        List of guild IDs where the bot responds in group channels.
                    Empty = respond in all guilds. Default: []
  prefix_required:  In group channels, only respond when @mentioned or when
                    the message starts with the command_prefix.
                    Default: true (ignore messages that don't mention or prefix)
  command_prefix:   Text prefix that triggers the bot in group channels.
                    Default: "!"
  max_reply_length: Discord message length cap before chunking. Default: 1900
  typing_indicator: Show "Bot is typing..." while the agent thinks. Default: true

Token setup:
  export DISCORD_BOT_TOKEN=your-bot-token-here

Required bot intents (Discord Developer Portal):
  - Message Content Intent (privileged — must be enabled manually)
  - Server Members Intent (optional but helpful for username resolution)

Finding your Discord user ID:
  Enable Developer Mode in Discord (Settings → Advanced → Developer Mode),
  then right-click your username and select "Copy User ID".
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

import discord

from contracts import (
    AgentError,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    ContentType,
    InboundMessage,
    Platform,
    SessionKey,
    UserIdentity,
)

if TYPE_CHECKING:
    from router import Router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS = {
    "token_env": "DISCORD_BOT_TOKEN",
    "allowed_users": [],
    "dm_enabled": True,
    "guild_ids": [],
    "prefix_required": True,
    "command_prefix": "!",
    "max_reply_length": 1900,
    "typing_indicator": True,
}


# ---------------------------------------------------------------------------
# Reply accumulator
#
# Collects streaming chunks into a full reply, then sends to Discord.
# Flushes immediately on AgentTextFinal or AgentError.
# ---------------------------------------------------------------------------

class _ReplyAccumulator:
    """Accumulates streamed agent text and flushes to a Discord channel."""

    def __init__(self, channel: discord.abc.Messageable, max_len: int) -> None:
        self._channel = channel
        self._max_len = max_len
        self._buf: list[str] = []
        self._done = asyncio.Event()
        self._error: str | None = None

    def feed(self, chunk: str) -> None:
        self._buf.append(chunk)

    def finish(self, final_text: str) -> None:
        """Called on AgentTextFinal — if final_text provided and buffer is
        empty (non-streaming mode), use it directly."""
        if final_text and not self._buf:
            self._buf.append(final_text)
        self._done.set()

    def error(self, message: str) -> None:
        self._error = message
        self._done.set()

    async def wait_and_send(self) -> None:
        await self._done.wait()
        if self._error:
            await self._channel.send(f"⚠️ {self._error}")
            return
        text = "".join(self._buf).strip()
        if not text:
            return
        # Chunk at max_reply_length to stay within Discord's 2000-char limit.
        for i in range(0, len(text), self._max_len):
            await self._channel.send(text[i : i + self._max_len])


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class DiscordBridge:
    def __init__(self, router: "Router", options: dict) -> None:
        self._router = router
        self._opts = {**DEFAULTS, **options}
        self._max_len: int = int(self._opts["max_reply_length"])
        self._typing: bool = bool(self._opts["typing_indicator"])
        self._prefix: str = str(self._opts["command_prefix"])
        self._prefix_required: bool = bool(self._opts["prefix_required"])
        self._dm_enabled: bool = bool(self._opts["dm_enabled"])
        self._guild_ids: set[int] = {int(g) for g in self._opts["guild_ids"]}

        # allowed_users: empty set = open access (warn at startup)
        raw_allowed: list = self._opts["allowed_users"]
        self._allowed_users: set[int] = {int(u) for u in raw_allowed}

        # session_key → _ReplyAccumulator for the active turn
        self._accumulators: dict[str, _ReplyAccumulator] = {}

        intents = discord.Intents.default()
        intents.message_content = True      # privileged — enable in Dev Portal
        intents.members = True              # needed for reliable username fetch
        self._client = discord.Client(intents=intents)

        self._client.event(self._on_ready)
        self._client.event(self._on_message)

    def _is_allowed(self, user_id: int) -> bool:
        """Return True if this user is permitted to interact with the bot.
        An empty allowlist means open access (with a startup warning)."""
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users

    # ------------------------------------------------------------------
    # Event handler registered with Router
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:
        session_key_str = str(event.session_key)
        acc = self._accumulators.get(session_key_str)
        if acc is None:
            logger.debug("Discord: received event for unknown session %s", session_key_str)
            return

        if isinstance(event, AgentTextChunk):
            acc.feed(event.text)

        elif isinstance(event, AgentTextFinal):
            acc.finish(event.text)

        elif isinstance(event, AgentToolCall):
            logger.debug(
                "Discord: tool call %s(%s) in session %s",
                event.tool_name,
                event.args,
                session_key_str,
            )

        elif isinstance(event, AgentToolResult):
            status = "error" if event.is_error else "ok"
            logger.debug(
                "Discord: tool result %s (%s) in session %s",
                event.tool_name,
                status,
                session_key_str,
            )

        elif isinstance(event, AgentError):
            acc.error(event.message)

    # ------------------------------------------------------------------
    # Discord callbacks
    # ------------------------------------------------------------------

    async def _on_ready(self) -> None:
        logger.info(
            "Discord bridge connected as %s (id=%s)",
            self._client.user,
            self._client.user.id if self._client.user else "?",
        )
        if not self._allowed_users:
            logger.warning(
                "Discord bridge: allowed_users is empty — the bot will respond "
                "to anyone. Set bridges.discord.options.allowed_users in config.yaml."
            )
        else:
            logger.info(
                "Discord bridge: %d allowed user(s) configured.", len(self._allowed_users)
            )

    async def _on_message(self, message: discord.Message) -> None:
        # Ignore own messages.
        if self._client.user and message.author.id == self._client.user.id:
            return

        # Access control — drop silently if user not on allowlist.
        if not self._is_allowed(message.author.id):
            logger.debug(
                "Discord: ignoring message from unauthorized user %s (%s)",
                message.author.id,
                message.author.display_name,
            )
            return

        is_dm = isinstance(message.channel, discord.DMChannel)

        # --- DM path ---
        if is_dm:
            if not self._dm_enabled:
                return
            session_key = SessionKey.dm(str(message.author.id))
            text = message.content.strip()

        # --- Guild path ---
        else:
            if self._guild_ids and message.guild and message.guild.id not in self._guild_ids:
                return

            if self._prefix_required:
                mentioned = (
                    self._client.user is not None
                    and self._client.user in message.mentions
                )
                prefixed = message.content.startswith(self._prefix)
                if not mentioned and not prefixed:
                    return

            session_key = SessionKey.group(Platform.DISCORD, str(message.channel.id))

            # Strip @mention and prefix from content.
            text = message.content
            if self._client.user:
                text = text.replace(f"<@{self._client.user.id}>", "").replace(
                    f"<@!{self._client.user.id}>", ""
                )
            if text.startswith(self._prefix):
                text = text[len(self._prefix):]
            text = text.strip()

        if not text:
            return

        author = UserIdentity(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.display_name,
        )
        msg = InboundMessage(
            session_key=session_key,
            author=author,
            content_type=ContentType.TEXT,
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
        )

        session_key_str = str(session_key)
        acc = _ReplyAccumulator(message.channel, self._max_len)
        self._accumulators[session_key_str] = acc

        # Typing indicator + push + wait + send, all in one task so the
        # on_message callback returns quickly and doesn't block the discord.py
        # event loop.
        asyncio.create_task(
            self._handle_turn(msg, message.channel, session_key_str, acc)
        )

    async def _handle_turn(
        self,
        msg: InboundMessage,
        channel: discord.abc.Messageable,
        session_key_str: str,
        acc: _ReplyAccumulator,
    ) -> None:
        try:
            accepted = await self._router.push(msg)
            if not accepted:
                await channel.send("⏳ I'm busy — please try again in a moment.")
                return

            if self._typing:
                async with channel.typing():
                    await acc.wait_and_send()
            else:
                await acc.wait_and_send()
        except Exception:
            logger.exception("Discord: error handling turn for session %s", session_key_str)
        finally:
            self._accumulators.pop(session_key_str, None)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        token_env = str(self._opts["token_env"])
        token = os.environ.get(token_env, "")
        if not token:
            raise RuntimeError(
                f"Discord bridge: env var '{token_env}' is not set. "
                "Export your bot token before starting."
            )

        self._router.register_platform_handler(Platform.DISCORD.value, self.handle_event)
        logger.info("Discord bridge: starting (token_env=%s)", token_env)
        await self._client.start(token)


# ---------------------------------------------------------------------------
# Loader entrypoint (called by main.py)
# ---------------------------------------------------------------------------

async def run(router: "Router") -> None:
    """Entry point called by main.py bridge loader."""
    bridge_cfg = router.config.bridges.get("discord")
    options: dict = bridge_cfg.options if bridge_cfg else {}
    bridge = DiscordBridge(router, options)
    await bridge.run()

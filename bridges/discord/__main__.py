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
  admin_users:      List of Discord user IDs (integers) permitted to use
                    /reset in group sessions. Empty = nobody can reset.
                    Default: []
  dm_enabled:       Allow DMs to the bot. Default: true
  guild_ids:        List of guild IDs where the bot responds in group channels.
                    Empty = respond in all guilds. Default: []
  prefix_required:  In group channels, only respond when @mentioned or when
                    the message starts with the command_prefix.
                    Default: true (ignore messages that don't mention or prefix)
  command_prefix:   Text prefix that triggers the bot in group channels.
                    Default: "!"
  reset_command:    Command string that triggers a session reset in group channels.
                    Default: "/reset"
  buffer_timeout_s: In group channels, seconds to wait after a non-trigger
                    message before flushing buffered messages anyway.
                    0 = disabled (only flush on trigger). Default: 0
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
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from contracts import (
    AgentError,
    AgentThinkingChunk,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    Attachment,
    content_type_for,
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
    "admin_users": [],
    "dm_enabled": True,
    "guild_ids": [],
    "prefix_required": True,
    "command_prefix": "!",
    "reset_command": "/reset",
    "buffer_timeout_s": 0,
    "max_reply_length": 1900,
    "typing_indicator": True,
    "typing_on_thinking": True,
    "typing_on_tools": True,
    "typing_on_reply": True,
}


# ---------------------------------------------------------------------------
# Mention humanization
#
# Converts Discord wire-format mentions (<@id> and <@!id>) to @username so
# the LLM sees readable text rather than raw snowflake IDs.
# Adapted from discord-mcp/discord_utils.py MentionProcessor.humanize_mentions.
# ---------------------------------------------------------------------------

async def _humanize_mentions(text: str, client: discord.Client) -> str:
    """Replace <@id> and <@!id> with @username in text."""
    pattern = re.compile(r"<@!?(\d+)>")

    async def _replace(match: re.Match) -> str:
        try:
            user = await client.fetch_user(int(match.group(1)))
            return f"@{user.name}"
        except Exception:
            return f"@[{match.group(1)}]"

    parts: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(text[last : m.start()])
        parts.append(await _replace(m))
        last = m.end()
    parts.append(text[last:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# GroupBuffer
#
# Accumulates non-triggering messages in a group channel and flushes them
# as a single attributed InboundMessage when a trigger arrives (or on timeout).
# ---------------------------------------------------------------------------

@dataclass
class _BufferedLine:
    user_id: str
    display_name: str
    text: str


class GroupBuffer:
    """Per-channel message buffer."""

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s          # 0 = disabled
        self._lines: list[_BufferedLine] = []
        self._flush_task: asyncio.Task | None = None
        self._flush_callback = None

    def set_flush_callback(self, cb) -> None:
        self._flush_callback = cb

    def add(self, user_id: str, display_name: str, text: str) -> None:
        self._lines.append(_BufferedLine(user_id, display_name, text))
        if self._timeout_s > 0:
            self._reset_timer()

    def flush(
        self,
        trigger_user_id: str | None = None,
        trigger_display_name: str | None = None,
        trigger_text: str | None = None,
    ) -> list[_BufferedLine]:
        """Return accumulated lines + optional trigger line, then clear."""
        self._cancel_timer()
        lines = list(self._lines)
        if trigger_text and trigger_user_id and trigger_display_name:
            lines.append(_BufferedLine(trigger_user_id, trigger_display_name, trigger_text))
        self._lines.clear()
        return lines

    def clear(self) -> None:
        self._cancel_timer()
        self._lines.clear()

    def _reset_timer(self) -> None:
        self._cancel_timer()
        if self._flush_callback:
            self._flush_task = asyncio.create_task(self._timeout_flush())

    def _cancel_timer(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None

    async def _timeout_flush(self) -> None:
        try:
            await asyncio.sleep(self._timeout_s)
            if self._lines and self._flush_callback:
                await self._flush_callback()
        except asyncio.CancelledError:
            pass


def _format_buffer(lines: list[_BufferedLine]) -> str:
    """Format buffered lines as attributed multi-line text for the LLM."""
    return "\n".join(f"[{line.display_name}]: {line.text}" for line in lines)


# ---------------------------------------------------------------------------
# Reply accumulator
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
        self._typing_on_thinking: bool = bool(self._opts["typing_on_thinking"])
        self._typing_on_tools: bool = bool(self._opts["typing_on_tools"])
        self._typing_on_reply: bool = bool(self._opts["typing_on_reply"])
        self._prefix: str = str(self._opts["command_prefix"])
        self._prefix_required: bool = bool(self._opts["prefix_required"])
        self._reset_command: str = str(self._opts["reset_command"])
        self._dm_enabled: bool = bool(self._opts["dm_enabled"])
        self._guild_ids: set[int] = {int(g) for g in self._opts["guild_ids"]}
        self._buffer_timeout_s: float = float(self._opts["buffer_timeout_s"])

        raw_allowed: list = self._opts["allowed_users"]
        self._allowed_users: set[int] = {int(u) for u in raw_allowed}

        raw_admin: list = self._opts["admin_users"]
        self._admin_users: set[int] = {int(u) for u in raw_admin}

        # session_key_str → _ReplyAccumulator for the active turn
        self._accumulators: dict[str, _ReplyAccumulator] = {}
        # session_key_str → asyncio.Event signalling typing should be active
        self._typing_active: dict[str, asyncio.Event] = {}
        # channel_id → GroupBuffer
        self._group_buffers: dict[str, GroupBuffer] = {}

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self._client = discord.Client(intents=intents)

        self._client.event(self._on_ready)
        self._client.event(self._on_message)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admin_users

    def _get_or_create_buffer(self, channel_id: str) -> GroupBuffer:
        if channel_id not in self._group_buffers:
            self._group_buffers[channel_id] = GroupBuffer(self._buffer_timeout_s)
        return self._group_buffers[channel_id]

    def _strip_trigger(self, text: str) -> str:
        """Strip bot @mention and command prefix from message text."""
        if self._client.user:
            text = text.replace(f"<@{self._client.user.id}>", "")
            text = text.replace(f"<@!{self._client.user.id}>", "")
        if text.startswith(self._prefix):
            text = text[len(self._prefix):]
        return text.strip()

    async def _fetch_attachments(self, message: discord.Message) -> tuple:
        if not message.attachments:
            return ()
        fetched = []
        for a in message.attachments:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(a.url) as resp:
                        data = await resp.read()
                mime = a.content_type or "application/octet-stream"
                fetched.append(Attachment(filename=a.filename, data=data, mime_type=mime))
            except Exception:
                logger.warning("Discord: failed to download attachment %s", a.filename)
        return tuple(fetched)

    # ------------------------------------------------------------------
    # Event handler registered with Router
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:
        session_key_str = str(event.session_key)
        acc = self._accumulators.get(session_key_str)
        if acc is None:
            logger.debug("Discord: received event for unknown session %s", session_key_str)
            return

        typing_ev = self._typing_active.get(session_key_str)

        if isinstance(event, AgentThinkingChunk):
            if typing_ev and self._typing_on_thinking:
                typing_ev.set()
        elif isinstance(event, AgentTextChunk):
            if typing_ev and self._typing_on_reply:
                typing_ev.set()
            acc.feed(event.text)
        elif isinstance(event, AgentTextFinal):
            acc.finish(event.text)
        elif isinstance(event, AgentToolCall):
            if typing_ev and self._typing_on_tools:
                typing_ev.set()
            logger.debug("Discord: tool call %s in session %s", event.tool_name, session_key_str)
        elif isinstance(event, AgentToolResult):
            logger.debug(
                "Discord: tool result %s (%s) in session %s",
                event.tool_name, "error" if event.is_error else "ok", session_key_str,
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
        if not self._admin_users:
            logger.warning(
                "Discord bridge: admin_users is empty — nobody can use %s in group sessions.",
                self._reset_command,
            )

    async def _on_message(self, message: discord.Message) -> None:
        # Ignore own messages.
        if self._client.user and message.author.id == self._client.user.id:
            return

        # Access control.
        if not self._is_allowed(message.author.id):
            logger.debug(
                "Discord: ignoring message from unauthorized user %s (%s)",
                message.author.id, message.author.display_name,
            )
            return

        is_dm = isinstance(message.channel, discord.DMChannel)

        # ----------------------------------------------------------------
        # DM path — unchanged from original behaviour
        # ----------------------------------------------------------------
        if is_dm:
            if not self._dm_enabled:
                return
            session_key = SessionKey.dm(str(message.author.id))
            text = message.content.strip()
            attachments = await self._fetch_attachments(message)
            if not text and not attachments:
                return

            author = UserIdentity(
                platform=Platform.DISCORD,
                user_id=str(message.author.id),
                username=message.author.display_name,
            )
            msg = InboundMessage(
                session_key=session_key,
                author=author,
                content_type=content_type_for(text, bool(attachments)),
                text=text,
                message_id=str(message.id),
                timestamp=time.time(),
                attachments=attachments,
            )
            session_key_str = str(session_key)
            acc = _ReplyAccumulator(message.channel, self._max_len)
            self._accumulators[session_key_str] = acc
            asyncio.create_task(
                self._handle_turn(msg, message.channel, session_key_str, acc)
            )
            return

        # ----------------------------------------------------------------
        # Group channel path
        # ----------------------------------------------------------------
        if self._guild_ids and message.guild and message.guild.id not in self._guild_ids:
            return

        session_key = SessionKey.group(Platform.DISCORD, str(message.channel.id))
        channel_id = str(message.channel.id)
        buf = self._get_or_create_buffer(channel_id)
        raw_text = message.content.strip()

        # /reset — admin only
        if raw_text == self._reset_command:
            if self._is_admin(message.author.id):
                buf.clear()
                self._router.reset_session(session_key)
                await message.channel.send("✅ Session reset.")
                logger.info(
                    "Discord: group session %s reset by admin %s",
                    channel_id, message.author.id,
                )
            else:
                await message.channel.send("⛔ Only admins can reset the session.")
            return

        # Determine whether this message is a trigger (mention or prefix).
        mentioned = (
            self._client.user is not None
            and self._client.user in message.mentions
        )
        prefixed = raw_text.startswith(self._prefix)
        is_trigger = mentioned or prefixed

        if self._prefix_required and not is_trigger:
            # Non-trigger: buffer it for context.
            humanized = await _humanize_mentions(raw_text, self._client)

            async def _timeout_flush_cb(
                _buf=buf, _channel_id=channel_id,
                _session_key=session_key, _channel=message.channel,
            ):
                await self._flush_group_buffer(
                    _buf, _channel_id, _session_key, _channel,
                    trigger_user_id=None, trigger_display_name=None, trigger_text=None,
                )

            buf.set_flush_callback(_timeout_flush_cb)
            buf.add(str(message.author.id), message.author.display_name, humanized)
            logger.debug(
                "Discord: buffered non-trigger message from %s in channel %s",
                message.author.display_name, channel_id,
            )
            return

        # Trigger: strip bot mention/prefix, humanize remaining mentions, flush.
        stripped = self._strip_trigger(raw_text)
        humanized_trigger = await _humanize_mentions(stripped, self._client)
        attachments = await self._fetch_attachments(message)

        await self._flush_group_buffer(
            buf, channel_id, session_key, message.channel,
            trigger_user_id=str(message.author.id),
            trigger_display_name=message.author.display_name,
            trigger_text=humanized_trigger,
            attachments=attachments,
            trigger_message_id=str(message.id),
        )

    # ------------------------------------------------------------------
    # Group flush — assembles buffer into one InboundMessage and pushes it
    # ------------------------------------------------------------------

    async def _flush_group_buffer(
        self,
        buf: GroupBuffer,
        channel_id: str,
        session_key: SessionKey,
        channel: discord.abc.Messageable,
        trigger_user_id: str | None,
        trigger_display_name: str | None,
        trigger_text: str | None,
        attachments: tuple = (),
        trigger_message_id: str | None = None,
    ) -> None:
        lines = buf.flush(
            trigger_user_id=trigger_user_id,
            trigger_display_name=trigger_display_name,
            trigger_text=trigger_text,
        )
        if not lines and not attachments:
            return

        combined_text = _format_buffer(lines)

        # Author: trigger sender if present, else first buffered sender.
        if trigger_user_id:
            author_uid = trigger_user_id
            author_name = trigger_display_name or trigger_user_id
            msg_id = trigger_message_id or str(time.time_ns())
        else:
            first = lines[0] if lines else None
            author_uid = first.user_id if first else "unknown"
            author_name = first.display_name if first else "unknown"
            msg_id = str(time.time_ns())

        author = UserIdentity(
            platform=Platform.DISCORD,
            user_id=author_uid,
            username=author_name,
        )
        msg = InboundMessage(
            session_key=session_key,
            author=author,
            content_type=content_type_for(combined_text, bool(attachments)),
            text=combined_text,
            message_id=msg_id,
            timestamp=time.time(),
            attachments=attachments,
        )

        session_key_str = str(session_key)
        acc = _ReplyAccumulator(channel, self._max_len)
        self._accumulators[session_key_str] = acc
        asyncio.create_task(
            self._handle_turn(msg, channel, session_key_str, acc)
        )

    # ------------------------------------------------------------------
    # Turn handling
    # ------------------------------------------------------------------

    async def _typing_keepalive(
        self,
        channel: discord.abc.Messageable,
        active_event: asyncio.Event,
        done_event: asyncio.Event,
    ) -> None:
        while not done_event.is_set():
            await active_event.wait()
            if done_event.is_set():
                break
            try:
                await channel.typing().__aenter__()
            except Exception:
                pass
            try:
                await asyncio.wait_for(done_event.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                pass

    async def _handle_turn(
        self,
        msg: InboundMessage,
        channel: discord.abc.Messageable,
        session_key_str: str,
        acc: _ReplyAccumulator,
    ) -> None:
        done_event = asyncio.Event()
        typing_ev = asyncio.Event()
        self._typing_active[session_key_str] = typing_ev

        try:
            accepted = await self._router.push(msg)
            if not accepted:
                await channel.send("⏳ I'm busy — please try again in a moment.")
                return

            if self._typing:
                keepalive = asyncio.create_task(
                    self._typing_keepalive(channel, typing_ev, done_event)
                )
                try:
                    await acc.wait_and_send()
                finally:
                    done_event.set()
                    typing_ev.set()
                    keepalive.cancel()
            else:
                await acc.wait_and_send()
        except Exception:
            logger.exception("Discord: error handling turn for session %s", session_key_str)
        finally:
            done_event.set()
            self._accumulators.pop(session_key_str, None)
            self._typing_active.pop(session_key_str, None)

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

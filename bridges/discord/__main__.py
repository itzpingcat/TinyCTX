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

Thread branching:
  When a Discord thread is created inside a tracked channel, the bot creates a
  new DB branch forked off the channel turn that spawned the thread. The channel
  and thread then evolve independently — both can be active simultaneously. The
  thread agent sees the full channel history up to the fork point, plus whatever
  has happened inside the thread since then.

  Cursor persistence:
  All cursors (DMs, channels, threads) are persisted to
  workspace/cursors/discord.json so sessions survive bot restarts. The file maps
  cursor_key strings to DB node UUIDs:
    "dm:<user_id>"        → node_id
    "group:<channel_id>"  → node_id  (advances with each turn)
    "thread:<thread_id>"  → node_id  (advances with each turn)

  Message → node mapping for fork points:
  When a channel trigger message is processed, the DB node ID of the resulting
  user turn is recorded in workspace/cursors/discord_msg_nodes.json keyed by
  Discord message ID. When a thread is created from that message, its cursor
  is initialised to that node ID — branching the tree exactly at that turn.
  If the origin message isn't mapped (e.g. predates the bot), the thread falls
  back to the channel's current tail.

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
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
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
# Cursor store — persists all Discord cursors across restarts
# ---------------------------------------------------------------------------

class CursorStore:
    """
    Persists two JSON files under workspace/cursors/:

    discord.json          — cursor_key -> node_id
                            Keys: "dm:<uid>", "group:<cid>", "thread:<tid>"

    discord_msg_nodes.json — discord_message_id -> db_node_id
                            Records which DB node a channel trigger message
                            produced, so thread forks can branch accurately.
                            Capped at MAX_MSG_NODES entries (LRU-style trim).
    """

    MAX_MSG_NODES = 2000

    def __init__(self, cursors_dir: Path) -> None:
        self._dir           = cursors_dir
        self._cursor_file   = cursors_dir / "discord.json"
        self._msg_node_file = cursors_dir / "discord_msg_nodes.json"
        self._cursors:   dict[str, str] = self._load(self._cursor_file)
        self._msg_nodes: dict[str, str] = self._load(self._msg_node_file)

    # ------------------------------------------------------------------
    # Cursor map (cursor_key -> node_id)
    # ------------------------------------------------------------------

    def get(self, cursor_key: str) -> str | None:
        return self._cursors.get(cursor_key)

    def set(self, cursor_key: str, node_id: str) -> None:
        self._cursors[cursor_key] = node_id
        self._save(self._cursor_file, self._cursors)

    def all_cursors(self) -> dict[str, str]:
        return dict(self._cursors)

    # ------------------------------------------------------------------
    # Message → node map (discord_message_id -> db_node_id)
    # ------------------------------------------------------------------

    def get_msg_node(self, discord_message_id: str) -> str | None:
        return self._msg_nodes.get(discord_message_id)

    def set_msg_node(self, discord_message_id: str, node_id: str) -> None:
        self._msg_nodes[discord_message_id] = node_id
        # Trim to cap if needed (remove oldest entries)
        if len(self._msg_nodes) > self.MAX_MSG_NODES:
            overflow = len(self._msg_nodes) - self.MAX_MSG_NODES
            for key in list(self._msg_nodes.keys())[:overflow]:
                del self._msg_nodes[key]
        self._save(self._msg_node_file, self._msg_nodes)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _load(path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("CursorStore: corrupt file %s — starting fresh", path)
        return {}

    @staticmethod
    def _save(path: Path, data: dict) -> None:
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("CursorStore: failed to save %s", path)


# ---------------------------------------------------------------------------
# GroupBuffer
# ---------------------------------------------------------------------------

@dataclass
class _BufferedLine:
    user_id: str
    display_name: str
    text: str


class GroupBuffer:
    """Per-channel message buffer."""

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s
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
# DB helpers
# ---------------------------------------------------------------------------

def _open_db(router):
    from db import ConversationDB
    workspace = Path(router._config.workspace.path).expanduser().resolve()
    return ConversationDB(workspace / "agent.db")


def _make_session_node(db, cursor_key: str) -> str:
    """Create a new session-anchor node off the global root and return its id."""
    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content=f"session:{cursor_key}")
    return node.id


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class DiscordBridge:
    def __init__(self, router: "Router", options: dict) -> None:
        self._router = router
        self._opts   = {**DEFAULTS, **options}

        self._max_len:          int   = int(self._opts["max_reply_length"])
        self._typing:           bool  = bool(self._opts["typing_indicator"])
        self._typing_on_thinking: bool = bool(self._opts["typing_on_thinking"])
        self._typing_on_tools:  bool  = bool(self._opts["typing_on_tools"])
        self._typing_on_reply:  bool  = bool(self._opts["typing_on_reply"])
        self._prefix:           str   = str(self._opts["command_prefix"])
        self._prefix_required:  bool  = bool(self._opts["prefix_required"])
        self._reset_command:    str   = str(self._opts["reset_command"])
        self._dm_enabled:       bool  = bool(self._opts["dm_enabled"])
        self._guild_ids:        set[int] = {int(g) for g in self._opts["guild_ids"]}
        self._buffer_timeout_s: float = float(self._opts["buffer_timeout_s"])

        self._allowed_users: set[int] = {int(u) for u in self._opts["allowed_users"]}
        self._admin_users:   set[int] = {int(u) for u in self._opts["admin_users"]}

        # In-flight state (not persisted)
        self._accumulators:  dict[str, _ReplyAccumulator] = {}
        self._typing_active: dict[str, asyncio.Event]     = {}
        self._group_buffers: dict[str, GroupBuffer]       = {}

        # Persisted cursor store
        workspace   = Path(router._config.workspace.path).expanduser().resolve()
        cursors_dir = workspace / "cursors"
        cursors_dir.mkdir(parents=True, exist_ok=True)
        self._store = CursorStore(cursors_dir)

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members         = True
        self._client = discord.Client(intents=intents)

        self._client.event(self._on_ready)
        self._client.event(self._on_message)

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    def _get_cursor(self, cursor_key: str) -> str | None:
        return self._store.get(cursor_key)

    def _get_or_create_cursor(self, cursor_key: str) -> str:
        node_id = self._store.get(cursor_key)
        if node_id:
            return node_id
        db      = _open_db(self._router)
        node_id = _make_session_node(db, cursor_key)
        self._store.set(cursor_key, node_id)
        logger.info("Discord: created cursor %s -> %s", cursor_key, node_id)
        return node_id

    def _get_or_create_thread_cursor(self, thread_id: str, channel_id: str) -> str:
        """
        Return the cursor node_id for a thread, creating it if necessary.

        Fork logic:
          1. If the thread already has a persisted cursor, use it.
          2. Look up the Discord message that spawned the thread
             (thread.id == starter message id in Discord) in our msg->node map.
             If found, fork off that specific user-turn node — the thread
             inherits full channel history up to that moment.
          3. Fall back to the channel's current tail if no mapping exists.
          4. Fall back to a fresh root-anchored session if the channel has
             no cursor at all (e.g. bot never saw that channel).
        """
        cursor_key = f"thread:{thread_id}"
        node_id    = self._store.get(cursor_key)
        if node_id:
            return node_id

        # Try to fork from the specific message that created the thread.
        # In Discord, thread.id == id of the starter message.
        parent_node_id = self._store.get_msg_node(thread_id)

        if parent_node_id is None:
            # Fall back to wherever the channel currently is.
            parent_node_id = self._store.get(f"group:{channel_id}")

        if parent_node_id is None:
            # No channel context at all — create a fresh branch.
            db         = _open_db(self._router)
            node_id    = _make_session_node(db, cursor_key)
            logger.info(
                "Discord: thread %s has no known parent — created fresh branch %s",
                thread_id, node_id,
            )
        else:
            # Fork: the thread's initial cursor IS the parent node.
            # The first add() in this thread will create a child of parent_node_id.
            node_id = parent_node_id
            logger.info(
                "Discord: thread %s forked from node %s", thread_id, parent_node_id
            )

        self._store.set(cursor_key, node_id)
        return node_id

    def _advance_cursor(self, cursor_key: str, router_node_id: str) -> None:
        """
        After a turn completes, read the lane's current tail and persist it.
        router_node_id is the node_id the lane was opened with (may differ from
        tail after the turn writes new nodes).
        """
        lane = self._router._lane_router._lanes.get(router_node_id)
        if lane:
            new_id = lane.loop._tail_node_id
            if new_id and new_id != self._store.get(cursor_key):
                self._store.set(cursor_key, new_id)

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
        node_id = event.tail_node_id
        acc     = self._accumulators.get(node_id)
        if acc is None:
            logger.debug("Discord: received event for unknown cursor %s", node_id)
            return

        typing_ev = self._typing_active.get(node_id)

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
            logger.debug("Discord: tool call %s for cursor %s", event.tool_name, node_id)
        elif isinstance(event, AgentToolResult):
            logger.debug(
                "Discord: tool result %s (%s) for cursor %s",
                event.tool_name, "error" if event.is_error else "ok", node_id,
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
        if self._client.user and message.author.id == self._client.user.id:
            return

        if not self._is_allowed(message.author.id):
            logger.debug(
                "Discord: ignoring message from unauthorized user %s (%s)",
                message.author.id, message.author.display_name,
            )
            return

        # ── Thread message ────────────────────────────────────────────
        if isinstance(message.channel, discord.Thread):
            await self._handle_thread_message(message)
            return

        # ── DM ────────────────────────────────────────────────────────
        if isinstance(message.channel, discord.DMChannel):
            if not self._dm_enabled:
                return
            text        = message.content.strip()
            attachments = await self._fetch_attachments(message)
            if not text and not attachments:
                return

            cursor_key = f"dm:{message.author.id}"
            node_id    = self._get_or_create_cursor(cursor_key)
            author     = UserIdentity(
                platform=Platform.DISCORD,
                user_id=str(message.author.id),
                username=message.author.display_name,
            )
            msg = InboundMessage(
                tail_node_id=node_id,
                author=author,
                content_type=content_type_for(text, bool(attachments)),
                text=text,
                message_id=str(message.id),
                timestamp=time.time(),
                attachments=attachments,
            )
            acc = _ReplyAccumulator(message.channel, self._max_len)
            self._accumulators[node_id] = acc
            asyncio.create_task(
                self._handle_turn(msg, message.channel, node_id, acc, cursor_key)
            )
            return

        # ── Group channel ─────────────────────────────────────────────
        if self._guild_ids and message.guild and message.guild.id not in self._guild_ids:
            return

        channel_id = str(message.channel.id)
        cursor_key = f"group:{channel_id}"
        buf        = self._get_or_create_buffer(channel_id)
        raw_text   = message.content.strip()

        if raw_text == self._reset_command:
            if self._is_admin(message.author.id):
                buf.clear()
                node_id = self._get_cursor(cursor_key)
                if node_id:
                    self._router.reset_lane(node_id)
                await message.channel.send("✅ Session reset.")
                logger.info(
                    "Discord: group channel %s reset by admin %s",
                    channel_id, message.author.id,
                )
            else:
                await message.channel.send("⛔ Only admins can reset the session.")
            return

        mentioned  = self._client.user is not None and self._client.user in message.mentions
        prefixed   = raw_text.startswith(self._prefix)
        is_trigger = mentioned or prefixed

        if self._prefix_required and not is_trigger:
            humanized = await _humanize_mentions(raw_text, self._client)

            async def _timeout_flush_cb(
                _buf=buf, _channel_id=channel_id,
                _cursor_key=cursor_key, _channel=message.channel,
            ):
                await self._flush_group_buffer(
                    _buf, _channel_id, _cursor_key, _channel,
                    trigger_user_id=None, trigger_display_name=None, trigger_text=None,
                )

            buf.set_flush_callback(_timeout_flush_cb)
            buf.add(str(message.author.id), message.author.display_name, humanized)
            logger.debug(
                "Discord: buffered non-trigger message from %s in channel %s",
                message.author.display_name, channel_id,
            )
            return

        stripped          = self._strip_trigger(raw_text)
        humanized_trigger = await _humanize_mentions(stripped, self._client)
        attachments       = await self._fetch_attachments(message)

        await self._flush_group_buffer(
            buf, channel_id, cursor_key, message.channel,
            trigger_user_id=str(message.author.id),
            trigger_display_name=message.author.display_name,
            trigger_text=humanized_trigger,
            attachments=attachments,
            trigger_message_id=str(message.id),
        )

    # ------------------------------------------------------------------
    # Thread message handler
    # ------------------------------------------------------------------

    async def _handle_thread_message(self, message: discord.Message) -> None:
        thread     = message.channel  # discord.Thread
        thread_id  = str(thread.id)
        channel_id = str(thread.parent_id) if thread.parent_id else ""
        cursor_key = f"thread:{thread_id}"

        # In threads, respond to every message (no trigger gating).
        # Threads are already opt-in — you have to come here intentionally.
        text        = message.content.strip()
        attachments = await self._fetch_attachments(message)
        if not text and not attachments:
            return

        node_id = self._get_or_create_thread_cursor(thread_id, channel_id)
        author  = UserIdentity(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.display_name,
        )
        msg = InboundMessage(
            tail_node_id=node_id,
            author=author,
            content_type=content_type_for(text, bool(attachments)),
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
            attachments=attachments,
        )
        acc = _ReplyAccumulator(message.channel, self._max_len)
        self._accumulators[node_id] = acc
        asyncio.create_task(
            self._handle_turn(msg, message.channel, node_id, acc, cursor_key)
        )

    # ------------------------------------------------------------------
    # Group channel flush
    # ------------------------------------------------------------------

    async def _flush_group_buffer(
        self,
        buf: GroupBuffer,
        channel_id: str,
        cursor_key: str,
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
        node_id       = self._get_or_create_cursor(cursor_key)

        if trigger_user_id:
            author_uid  = trigger_user_id
            author_name = trigger_display_name or trigger_user_id
            msg_id      = trigger_message_id or str(time.time_ns())
        else:
            first       = lines[0] if lines else None
            author_uid  = first.user_id if first else "unknown"
            author_name = first.display_name if first else "unknown"
            msg_id      = str(time.time_ns())

        author = UserIdentity(
            platform=Platform.DISCORD,
            user_id=author_uid,
            username=author_name,
        )
        msg = InboundMessage(
            tail_node_id=node_id,
            author=author,
            content_type=content_type_for(combined_text, bool(attachments)),
            text=combined_text,
            message_id=msg_id,
            timestamp=time.time(),
            attachments=attachments,
        )

        acc = _ReplyAccumulator(channel, self._max_len)
        self._accumulators[node_id] = acc
        asyncio.create_task(
            self._handle_turn(
                msg, channel, node_id, acc, cursor_key,
                record_msg_node=trigger_message_id,
            )
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
        node_id: str,
        acc: _ReplyAccumulator,
        cursor_key: str | None = None,
        record_msg_node: str | None = None,
    ) -> None:
        """
        Execute one agent turn.

        record_msg_node: if set, this is the Discord message ID of the trigger
        message. After the user turn is written to the DB but before the agent
        replies, we snapshot the lane's tail (= the user turn node) and store
        it in the msg->node map so future threads can fork from it precisely.
        """
        done_event = asyncio.Event()
        typing_ev  = asyncio.Event()
        self._typing_active[node_id] = typing_ev

        try:
            accepted = await self._router.push(msg)
            if not accepted:
                await channel.send("⏳ I'm busy — please try again in a moment.")
                return

            # Capture the user-turn node ID immediately after push.
            # At this point the lane has ingested the user message and written
            # its DB node, but hasn't replied yet — so tail == user turn node.
            if record_msg_node:
                lane = self._router._lane_router._lanes.get(node_id)
                if lane:
                    user_turn_node_id = lane.loop._tail_node_id
                    if user_turn_node_id:
                        self._store.set_msg_node(record_msg_node, user_turn_node_id)
                        logger.debug(
                            "Discord: mapped message %s -> node %s",
                            record_msg_node, user_turn_node_id,
                        )

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

            # Persist the advanced cursor after the full turn completes.
            if cursor_key:
                self._advance_cursor(cursor_key, node_id)

        except Exception:
            logger.exception("Discord: error handling turn for cursor %s", node_id)
        finally:
            done_event.set()
            self._accumulators.pop(node_id, None)
            self._typing_active.pop(node_id, None)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        token_env = str(self._opts["token_env"])
        token     = os.environ.get(token_env, "")
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

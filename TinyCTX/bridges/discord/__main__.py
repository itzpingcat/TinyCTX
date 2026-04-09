"""
bridges/discord/__main__.py — Discord bridge for TinyCTX.

Uses discord.py (pip install discord.py).

Config (in config.yaml under bridges.discord.options):
  token_env:         Name of the env var holding the bot token.
                     Default: DISCORD_BOT_TOKEN
  allowed_users_dm:  Allowlist of Discord user IDs (integers) permitted to DM
                     the bot. Empty list = open to everyone.
                     Default: []  (WARNING: open access — set this!)
  allowed_servers:   Map of server (guild) ID to a list of allowed channel IDs.
                     Only servers present in this map are served. For each
                     server, an empty channel list means all channels are
                     allowed; a non-empty list restricts to those channels.
                     Default: {}  (WARNING: no servers will be served — set this!)
                     Example:
                       allowed_servers:
                         123456789012345678: []          # all channels
                         987654321098765432:             # specific channels only
                           - 111111111111111111
                           - 222222222222222222
  admin_users:       List of Discord user IDs (integers) permitted to use
                     /reset in group sessions. Empty = nobody can reset.
                     Default: []
  dm_enabled:        Allow DMs to the bot. Default: true
  prefix_required:   In group channels, only respond when @mentioned or when
                     the message starts with the command_prefix.
                     Default: true (ignore messages that don't mention or prefix)
  command_prefix:    Text prefix that triggers the bot in group channels.
                     Default: "!"
  reset_command:     Command string that triggers a session reset in group channels.
                     Default: "/reset"
  buffer_timeout_s:  In group channels, seconds to wait after a non-trigger
                     message before flushing buffered messages anyway.
                     0 = disabled (only flush on trigger). Default: 0
  buffer_head_lines: When truncating a large burst, keep this many messages
                     from the START (topic context). Default: 2
  buffer_tail_lines: Messages to keep from the END of a truncated burst
                     (closest to the trigger). Default: 10
                     Omitted middle is replaced with:
                     "... [N messages not shown] ..."
                     Trigger detection, buffering, and truncation are all
                     handled by GroupLane in router.py via GroupPolicy.
  max_reply_length:  Discord message length cap before chunking. Default: 1900
  typing_indicator:  Show "Bot is typing..." while the agent thinks. Default: true

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
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from TinyCTX.contracts import (
    ActivationMode,
    AgentError,
    AgentThinkingChunk,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    Attachment,
    content_type_for,
    GroupPolicy,
    InboundMessage,
    Platform,
    UserIdentity,
)

if TYPE_CHECKING:
    from TinyCTX.router import Router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS = {
    "token_env": "DISCORD_BOT_TOKEN",
    "allowed_users_dm": [],
    "allowed_servers": {},
    "admin_users": [],
    "dm_enabled": True,
    "prefix_required": True,
    "command_prefix": "!",
    "reset_command": "/reset",
    "buffer_timeout_s": 0,
    "buffer_head_lines": 2,
    "buffer_tail_lines": 10,
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
    from TinyCTX.db import ConversationDB
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

        # allowed_servers: {guild_id: set[channel_id]} — empty set = all channels
        raw_servers = self._opts["allowed_servers"]
        self._allowed_servers: dict[int, set[int]] = {
            int(guild_id): {int(c) for c in channels}
            for guild_id, channels in raw_servers.items()
        }
        self._buffer_timeout_s:  float = float(self._opts["buffer_timeout_s"])
        self._buffer_head_lines: int   = int(self._opts["buffer_head_lines"])
        self._buffer_tail_lines: int   = int(self._opts["buffer_tail_lines"])

        self._allowed_users_dm: set[int] = {int(u) for u in self._opts["allowed_users_dm"]}
        self._admin_users:   set[int] = {int(u) for u in self._opts["admin_users"]}

        # In-flight state (not persisted)
        self._accumulators:  dict[str, _ReplyAccumulator] = {}
        self._typing_active: dict[str, asyncio.Event]     = {}

        # Persisted cursor store
        workspace   = Path(router._config.workspace.path).expanduser().resolve()
        cursors_dir = workspace / "cursors"
        cursors_dir.mkdir(parents=True, exist_ok=True)
        self._store = CursorStore(cursors_dir)

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members         = True
        self._client = discord.Client(intents=intents)

        # Assign directly by name so discord.py dispatches to `on_ready` /
        # `on_message`. Using client.event() would register under the method's
        # __name__ (`_on_ready`, `_on_message`) — with the leading underscore —
        # causing discord to silently drop every event because it looks for the
        # un-prefixed names.
        self._client.on_ready = self._on_ready
        self._client.on_message = self._on_message

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

    def _is_allowed_dm(self, user_id: int) -> bool:
        if not self._allowed_users_dm:
            return True
        return user_id in self._allowed_users_dm

    def _is_allowed_server(self, guild_id: int, channel_id: int) -> bool:
        """Return True if the guild is in allowed_servers and the channel is permitted."""
        if guild_id not in self._allowed_servers:
            return False
        allowed_channels = self._allowed_servers[guild_id]
        # Empty set means all channels in that server are allowed.
        return not allowed_channels or channel_id in allowed_channels

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admin_users

    def _build_group_policy(self) -> GroupPolicy:
        """Build the GroupPolicy for this channel from bridge config."""
        activation = ActivationMode.ALWAYS if not self._prefix_required else ActivationMode.MENTION
        bot_id     = str(self._client.user.id) if self._client.user else ""
        return GroupPolicy(
            activation=activation,
            trigger_prefix=self._prefix,
            bot_mxid=f"<@{bot_id}>",        # Discord mention format
            bot_localpart=f"<@!{bot_id}>",   # legacy mention format
            buffer_timeout_s=self._buffer_timeout_s,
            buffer_head_lines=self._buffer_head_lines,
            buffer_tail_lines=self._buffer_tail_lines,
        )

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
        node_id = event.lane_node_id   # stable lane key � never advances during a turn
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
        if not self._allowed_users_dm:
            logger.warning(
                "Discord bridge: allowed_users_dm is empty — the bot will respond "
                "to DMs from anyone. Set bridges.discord.options.allowed_users_dm in config.yaml."
            )
        if not self._allowed_servers:
            logger.warning(
                "Discord bridge: allowed_servers is empty — the bot will not respond "
                "in any server. Set bridges.discord.options.allowed_servers in config.yaml."
            )
        if not self._admin_users:
            logger.warning(
                "Discord bridge: admin_users is empty — nobody can use %s in group sessions.",
                self._reset_command,
            )

    async def _on_message(self, message: discord.Message) -> None:
        if self._client.user and message.author.id == self._client.user.id:
            return

        # Per-context access checks happen below (DM vs group).

        # ── Thread message ────────────────────────────────────────────
        if isinstance(message.channel, discord.Thread):
            await self._handle_thread_message(message)
            return

        # ── DM ────────────────────────────────────────────────────────
        if isinstance(message.channel, discord.DMChannel):
            if not self._dm_enabled:
                return
            if not self._is_allowed_dm(message.author.id):
                logger.debug(
                    "Discord: ignoring DM from unauthorized user %s (%s)",
                    message.author.id, message.author.display_name,
                )
                return
            text        = message.content.strip()
            attachments = await self._fetch_attachments(message)
            if not text and not attachments:
                return

            # Slash commands in DMs also go through the module registry.
            if text.startswith("/"):
                dm_cursor_key = f"dm:{message.author.id}"
                dm_node_id    = self._get_or_create_cursor(dm_cursor_key)
                ctx = {
                    "channel": message.channel,
                    "message": message,
                    "guild":   None,
                    "bridge":  self,
                    "router":  self._router,
                    "cursor":  dm_node_id,
                }
                handled = await self._router.commands.dispatch(text, ctx)
                if handled:
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
        # Trigger detection, mention stripping, buffering, and truncation are
        # all handled by GroupLane in router.py via the GroupPolicy we attach.
        if not message.guild or not self._is_allowed_server(
            message.guild.id, message.channel.id
        ):
            logger.debug(
                "Discord: ignoring message in unallowed guild/channel %s/%s",
                getattr(message.guild, "id", None),
                message.channel.id,
            )
            return

        channel_id = str(message.channel.id)
        cursor_key = f"group:{channel_id}"
        raw_text   = message.content.strip()

        if raw_text == self._reset_command:
            if self._is_admin(message.author.id):
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

        # ── Slash commands (module registry) ─────────────────────────
        if raw_text.startswith("/"):
            node_id = self._get_or_create_cursor(cursor_key)
            ctx = {
                "channel":  message.channel,
                "message":  message,
                "guild":    message.guild,
                "bridge":   self,
                "router":   self._router,
                "cursor":   node_id,
            }
            handled = await self._router.commands.dispatch(raw_text, ctx)
            if handled:
                return

        text        = await _humanize_mentions(raw_text, self._client)
        attachments = await self._fetch_attachments(message)
        if not text and not attachments:
            return

        node_id = self._get_or_create_cursor(cursor_key)
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
            group_policy=self._build_group_policy(),
        )
        acc = _ReplyAccumulator(message.channel, self._max_len)
        self._accumulators[node_id] = acc
        asyncio.create_task(
            self._handle_turn(
                msg, message.channel, node_id, acc, cursor_key,
                record_msg_node=str(message.id),
            )
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

        # Slash commands in threads also go through the module registry.
        if text.startswith("/"):
            ctx = {
                "channel": message.channel,
                "message": message,
                "guild":   message.guild,
                "bridge":  self,
                "router":  self._router,
                "cursor":  node_id,
            }
            handled = await self._router.commands.dispatch(text, ctx)
            if handled:
                return
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
    # Turn handling
    # ------------------------------------------------------------------

    async def _typing_keepalive(
        self,
        channel: discord.abc.Messageable,
        active_event: asyncio.Event,
        done_event: asyncio.Event,
    ) -> None:
        # Wait until the first real activity (thinking/tool/reply chunk) before
        # showing the typing indicator — avoids a spurious indicator on errors
        # that resolve instantly.
        await active_event.wait()
        while not done_event.is_set():
            try:
                # Use the context manager correctly so __aexit__ is always
                # called.  The old code called __aenter__ without __aexit__,
                # leaking discord.py's internal keepalive task on every cycle.
                async with channel.typing():
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=8.0)
                    except asyncio.TimeoutError:
                        pass  # Loop back and send another typing burst
            except Exception:
                await asyncio.sleep(1)

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

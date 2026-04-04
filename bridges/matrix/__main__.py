"""
bridges/matrix/__main__.py — Matrix bridge for TinyCTX.

Uses matrix-nio (pip install matrix-nio).

Config (in config.yaml under bridges.matrix.options):
  homeserver:       Full URL of your homeserver, e.g. https://matrix.org
  username:         Full Matrix ID, e.g. @yourbot:matrix.org
  password_env:     Name of the env var holding the account password.
                    Default: MATRIX_PASSWORD
  device_name:      Device display name registered with the server.
                    Default: TinyCTX
  store_path:       Path (relative to workspace) for nio's E2EE key store.
                    Default: matrix_store
  allowed_users:    Allowlist of Matrix user IDs (full MXIDs, e.g.
                    "@you:matrix.org") permitted to interact with the bot.
                    Empty list = open to everyone.
                    Default: []  (WARNING: open access — set this!)
  admin_users:      List of Matrix user IDs (full MXIDs) permitted to use
                    /reset in group rooms. Empty = nobody can reset.
                    Default: []
  dm_enabled:       Respond to 1-on-1 rooms. Default: true
  room_ids:         Whitelist of room IDs to respond in. Empty = all rooms
                    the bot is joined to. Default: []
  prefix_required:  In non-DM rooms, only respond when @mentioned or when
                    the message starts with command_prefix. Default: true
  command_prefix:   Text prefix that triggers the bot in rooms.
                    Default: "!"
  reset_command:    Command string that triggers a session reset in group rooms.
                    Default: "/reset"
  buffer_timeout_s: In group rooms, seconds to wait after a non-trigger
                    message before flushing buffered messages anyway.
                    0 = disabled (only flush on trigger). Default: 0
  max_reply_length: Max characters per Matrix message before chunking.
                    Default: 16000
  sync_timeout_ms:  Long-poll timeout per /sync call in ms. Default: 30000

Password setup:
  export MATRIX_PASSWORD=your-password-here

Required:
  pip install matrix-nio
  For E2EE support: pip install matrix-nio[e2e]
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginResponse,
    MatrixRoom,
    RoomMessageText,
    SyncError,
)
try:
    from nio import (
        RoomMessageAudio,
        RoomMessageFile,
        RoomMessageImage,
        RoomMessageMedia,
        RoomMessageVideo,
    )
    _HAS_MEDIA_EVENTS = True
except ImportError:
    RoomMessageAudio = RoomMessageFile = RoomMessageImage = None  # type: ignore
    RoomMessageMedia = RoomMessageVideo = None                     # type: ignore
    _HAS_MEDIA_EVENTS = False

from contracts import (
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
    from router import Router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS = {
    "password_env": "MATRIX_PASSWORD",
    "device_name": "TinyCTX",
    "store_path": "matrix_store",
    "allowed_users": [],
    "admin_users": [],
    "dm_enabled": True,
    "room_ids": [],
    "prefix_required": True,
    "command_prefix": "!",
    "reset_command": "/reset",
    "buffer_timeout_s": 0,
    "max_reply_length": 16000,
    "sync_timeout_ms": 30000,
    "typing_indicator": True,
    "typing_on_thinking": True,
    "typing_on_tools": True,
    "typing_on_reply": True,
}


# ---------------------------------------------------------------------------
# Mention humanization
#
# Matrix plain-body mentions are already human-readable (@user:server), but
# formatted bodies use HTML <a href="https://matrix.to/#/@user:server">Name</a>.
# We normalize both to @localpart for LLM readability.
#
# Note: trigger detection, prefix/mention stripping, and non-trigger
# buffering are handled by GroupLane in router.py via GroupPolicy.
# This bridge only needs to humanize HTML anchor mentions before passing
# the raw text down (Matrix-specific formatting concern).
# ---------------------------------------------------------------------------

_MATRIX_HTML_MENTION = re.compile(
    r'<a\s+href="https://matrix\.to/#/(@[^"]+)"[^>]*>([^<]*)</a>',
    re.IGNORECASE,
)
_MATRIX_PLAIN_MXID = re.compile(r"@[\w\-.]+:[\w\-.]+")


def _humanize_matrix_mentions(text: str, own_mxid: str) -> str:
    """
    Normalize Matrix mention formats to @localpart for LLM readability.
    - HTML anchor mentions -> @localpart (from the MXID in href)
    - Full MXIDs (@user:server) -> @localpart
    The bot's own MXID is stripped entirely (it's the trigger, not context).
    """
    # Strip HTML anchor mentions, replacing with @localpart.
    def _replace_html(m: re.Match) -> str:
        mxid = m.group(1)           # e.g. @alice:matrix.org
        if mxid == own_mxid:
            return ""
        return f"@{mxid.split(':')[0].lstrip('@')}"

    text = _MATRIX_HTML_MENTION.sub(_replace_html, text)

    # Normalize remaining plain MXIDs (@user:server -> @localpart),
    # stripping the bot's own MXID.
    def _replace_plain(m: re.Match) -> str:
        mxid = m.group(0)
        if mxid == own_mxid:
            return ""
        return f"@{mxid.split(':')[0].lstrip('@')}"

    text = _MATRIX_PLAIN_MXID.sub(_replace_plain, text)
    return text.strip()


# ---------------------------------------------------------------------------
# Reply accumulator
# ---------------------------------------------------------------------------

class _ReplyAccumulator:
    def __init__(self, max_len: int) -> None:
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

    async def wait(self) -> list[str]:
        await self._done.wait()
        if self._error:
            return [f"⚠️ {self._error}"]
        text = "".join(self._buf).strip()
        if not text:
            return []
        return [text[i : i + self._max_len] for i in range(0, len(text), self._max_len)]


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class MatrixBridge:
    def __init__(self, router: "Router", options: dict) -> None:
        self._router = router
        self._opts = {**DEFAULTS, **options}

        self._homeserver: str = str(self._opts["homeserver"])
        self._username: str = str(self._opts["username"])
        self._max_len: int = int(self._opts["max_reply_length"])
        self._prefix: str = str(self._opts["command_prefix"])
        self._prefix_required: bool = bool(self._opts["prefix_required"])
        self._reset_command: str = str(self._opts["reset_command"])
        self._dm_enabled: bool = bool(self._opts["dm_enabled"])
        self._room_ids: set[str] = set(self._opts["room_ids"])
        self._sync_timeout: int = int(self._opts["sync_timeout_ms"])
        self._typing: bool = bool(self._opts["typing_indicator"])
        self._typing_on_thinking: bool = bool(self._opts["typing_on_thinking"])
        self._typing_on_tools: bool = bool(self._opts["typing_on_tools"])
        self._typing_on_reply: bool = bool(self._opts["typing_on_reply"])
        self._buffer_timeout_s: float = float(self._opts["buffer_timeout_s"])

        raw_allowed: list = self._opts["allowed_users"]
        self._allowed_users: set[str] = {str(u) for u in raw_allowed}

        raw_admin: list = self._opts["admin_users"]
        self._admin_users: set[str] = {str(u) for u in raw_admin}

        workspace = str(router.config.workspace.path)
        raw_store = str(self._opts["store_path"])
        self._store_path = raw_store if os.path.isabs(raw_store) else os.path.join(workspace, raw_store)
        os.makedirs(self._store_path, exist_ok=True)

        # node_id → _ReplyAccumulator
        self._accumulators: dict[str, _ReplyAccumulator] = {}
        # node_id → asyncio.Event signalling typing should be active
        self._typing_active: dict[str, asyncio.Event] = {}
        # sender+room_id → pending Attachments (media arrives before text in Matrix)
        self._pending_attachments: dict[str, list[Attachment]] = {}
        # cursor persistence: room_id / dm sender_id → node_id
        self._cursors: dict[str, str] = {}

        self._client: AsyncClient | None = None
        self._own_user_id: str = ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_allowed(self, sender: str) -> bool:
        if not self._allowed_users:
            return True
        return sender in self._allowed_users

    def _is_admin(self, sender: str) -> bool:
        return sender in self._admin_users

    def _is_dm_room(self, room: MatrixRoom) -> bool:
        return room.member_count == 2

    def _display_name(self, room: MatrixRoom, sender: str) -> str:
        return room.user_name(sender) or sender.split(":")[0].lstrip("@")

    def _build_group_policy(self) -> GroupPolicy:
        """Build the GroupPolicy for this room from bridge config."""
        localpart = self._username.split(":")[0].lstrip("@")
        activation = ActivationMode.ALWAYS if not self._prefix_required else ActivationMode.MENTION
        return GroupPolicy(
            activation=activation,
            trigger_prefix=self._prefix,
            bot_mxid=self._username,
            bot_localpart=localpart,
            buffer_timeout_s=self._buffer_timeout_s,
        )

    # ------------------------------------------------------------------
    # Event handler registered with Router
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:
        node_id = event.tail_node_id
        acc = self._accumulators.get(node_id)
        if acc is None:
            logger.debug("Matrix: received event for unknown cursor %s", node_id)
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
            logger.debug("Matrix: tool call %s for cursor %s", event.tool_name, node_id)
        elif isinstance(event, AgentToolResult):
            logger.debug(
                "Matrix: tool result %s (%s) for cursor %s",
                event.tool_name, "error" if event.is_error else "ok", node_id,
            )
        elif isinstance(event, AgentError):
            acc.error(event.message)

    # ------------------------------------------------------------------
    # nio message callback
    # ------------------------------------------------------------------

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if event.sender == self._own_user_id:
            return

        # Ignore replayed history from before startup.
        age_ms = getattr(event, "server_timestamp", 0)
        now_ms = int(time.time() * 1000)
        if age_ms and (now_ms - age_ms) > 60_000:
            return

        if not self._is_allowed(event.sender):
            logger.debug("Matrix: ignoring message from unauthorized user %s", event.sender)
            return

        is_dm = self._is_dm_room(room)

        if is_dm and not self._dm_enabled:
            return

        if self._room_ids and room.room_id not in self._room_ids:
            return

        body = event.body.strip()

        # ----------------------------------------------------------------
        # DM path
        # ----------------------------------------------------------------
        if is_dm:
            att_key     = f"{event.sender}:{room.room_id}"
            attachments = tuple(self._pending_attachments.pop(att_key, []))
            cursor_key  = f"dm:{event.sender}"
            node_id     = self._get_or_create_cursor(cursor_key)

            # Slash commands in DMs go through the module registry.
            if body.startswith("/"):
                ctx = {
                    "room":    room,
                    "event":   event,
                    "bridge":  self,
                    "router":  self._router,
                    "cursor":  node_id,
                    "send":    self._send,
                }
                handled = await self._router.commands.dispatch(body, ctx)
                if handled:
                    return

            author = UserIdentity(
                platform=Platform.MATRIX,
                user_id=event.sender,
                username=self._display_name(room, event.sender),
            )
            msg = InboundMessage(
                tail_node_id=node_id,
                author=author,
                content_type=content_type_for(body, bool(attachments)),
                text=body,
                message_id=event.event_id,
                timestamp=time.time(),
                attachments=attachments,
            )
            acc = _ReplyAccumulator(self._max_len)
            self._accumulators[node_id] = acc
            asyncio.create_task(
                self._handle_turn(msg, room.room_id, node_id, acc, cursor_key)
            )
            return

        # ----------------------------------------------------------------
        # Group room path
        # ----------------------------------------------------------------
        cursor_key = f"group:{room.room_id}"

        # /reset — admin only (handled before routing)
        if body == self._reset_command:
            if self._is_admin(event.sender):
                node_id = self._cursors.get(cursor_key)
                if node_id:
                    self._router.reset_lane(node_id)
                await self._send(room.room_id, "✅ Session reset.")
                logger.info(
                    "Matrix: group room %s reset by admin %s",
                    room.room_id, event.sender,
                )
            else:
                await self._send(room.room_id, "⛔ Only admins can reset the session.")
            return

        # Slash commands (module registry) — dispatched before trigger gating.
        if body.startswith("/"):
            node_id = self._get_or_create_cursor(cursor_key)
            ctx = {
                "room":   room,
                "event":  event,
                "bridge": self,
                "router": self._router,
                "cursor": node_id,
                "send":   self._send,
            }
            handled = await self._router.commands.dispatch(body, ctx)
            if handled:
                return

        # Humanize HTML mention markup (Matrix-specific formatting only).
        # Trigger detection and stripping are handled by GroupLane via GroupPolicy.
        humanized_body = _humanize_matrix_mentions(body, self._own_user_id)

        display     = self._display_name(room, event.sender)
        att_key     = f"{event.sender}:{room.room_id}"
        attachments = tuple(self._pending_attachments.pop(att_key, []))
        node_id     = self._get_or_create_cursor(cursor_key)

        author = UserIdentity(
            platform=Platform.MATRIX,
            user_id=event.sender,
            username=display,
        )
        msg = InboundMessage(
            tail_node_id=node_id,
            author=author,
            content_type=content_type_for(humanized_body, bool(attachments)),
            text=humanized_body,
            message_id=event.event_id,
            timestamp=time.time(),
            attachments=attachments,
            group_policy=self._build_group_policy(),
        )
        acc = _ReplyAccumulator(self._max_len)
        self._accumulators[node_id] = acc
        asyncio.create_task(
            self._handle_turn(msg, room.room_id, node_id, acc, cursor_key)
        )

    async def _on_media(self, room: MatrixRoom, event) -> None:
        """Buffer media attachments — Matrix sends these as separate events from text."""
        if event.sender == self._own_user_id:
            return
        if not self._is_allowed(event.sender):
            return
        if self._client is None:
            return

        url: str = getattr(event, "url", "") or ""
        filename: str = (
            (event.source.get("content") or {}).get("body")
            or getattr(event, "body", None)
            or "attachment"
        )
        info: dict = getattr(event, "info", None) or {}
        mime: str = info.get("mimetype", "application/octet-stream")

        if not url:
            logger.warning("Matrix: media event from %s has no url", event.sender)
            return

        try:
            resp = await self._client.download(url)
            data: bytes = resp.body if hasattr(resp, "body") else bytes(resp)
        except Exception:
            logger.warning("Matrix: failed to download media from %s", event.sender)
            return

        att = Attachment(filename=filename, data=data, mime_type=mime)
        key = f"{event.sender}:{room.room_id}"
        self._pending_attachments.setdefault(key, []).append(att)
        logger.debug("Matrix: buffered attachment %s (%s) from %s", filename, mime, event.sender)

    # ------------------------------------------------------------------
    # Turn handling
    # ------------------------------------------------------------------

    async def _typing_keepalive(
        self,
        room_id: str,
        active_event: asyncio.Event,
        done_event: asyncio.Event,
    ) -> None:
        while not done_event.is_set():
            await active_event.wait()
            if done_event.is_set():
                break
            if self._client:
                try:
                    await self._client.room_typing(room_id, typing_state=True, timeout=30000)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(done_event.wait(), timeout=25.0)
            except asyncio.TimeoutError:
                pass
        if self._client:
            try:
                await self._client.room_typing(room_id, typing_state=False)
            except Exception:
                pass

    def _get_or_create_cursor(self, cursor_key: str) -> str:
        """Return the node_id for a cursor_key, creating it in the DB if new."""
        if cursor_key in self._cursors:
            return self._cursors[cursor_key]
        from db import ConversationDB
        workspace   = Path(self._router._config.workspace.path).expanduser().resolve()
        db          = ConversationDB(workspace / "agent.db")
        root        = db.get_root()
        node        = db.add_node(parent_id=root.id, role="system", content=f"session:{cursor_key}")
        self._cursors[cursor_key] = node.id
        return node.id

    async def _handle_turn(
        self,
        msg: InboundMessage,
        room_id: str,
        node_id: str,
        acc: _ReplyAccumulator,
        cursor_key: str | None = None,
    ) -> None:
        done_event = asyncio.Event()
        typing_ev = asyncio.Event()
        self._typing_active[node_id] = typing_ev

        try:
            accepted = await self._router.push(msg)
            if not accepted:
                await self._send(room_id, "⏳ I'm busy — please try again in a moment.")
                return

            if self._typing:
                keepalive = asyncio.create_task(
                    self._typing_keepalive(room_id, typing_ev, done_event)
                )
                try:
                    chunks = await acc.wait()
                finally:
                    done_event.set()
                    typing_ev.set()
                    keepalive.cancel()
            else:
                chunks = await acc.wait()

            for chunk in chunks:
                await self._send(room_id, chunk)

            # Advance cursor after a successful turn.
            if cursor_key:
                lane = self._router._lane_router._lanes.get(node_id)
                if lane:
                    new_id = lane.loop._tail_node_id
                    if new_id and new_id != node_id:
                        self._cursors[cursor_key] = new_id
        except Exception:
            logger.exception("Matrix: error handling turn for cursor %s", node_id)
        finally:
            done_event.set()
            self._accumulators.pop(node_id, None)
            self._typing_active.pop(node_id, None)

    async def _send(self, room_id: str, text: str) -> None:
        if self._client is None:
            return
        await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        password_env = str(self._opts["password_env"])
        password = os.environ.get(password_env, "")
        if not password:
            raise RuntimeError(
                f"Matrix bridge: env var '{password_env}' is not set. "
                "Export your Matrix account password before starting."
            )

        device_name = str(self._opts["device_name"])
        config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=False,
        )
        client = AsyncClient(
            homeserver=self._homeserver,
            user=self._username,
            store_path=self._store_path,
            config=config,
        )
        self._client = client

        logger.info("Matrix bridge: logging in as %s", self._username)
        resp = await client.login(password=password, device_name=device_name)
        if not isinstance(resp, LoginResponse):
            raise RuntimeError(f"Matrix login failed: {resp}")

        self._own_user_id = client.user_id
        logger.info("Matrix bridge: logged in, user_id=%s", self._own_user_id)

        if not self._allowed_users:
            logger.warning(
                "Matrix bridge: allowed_users is empty — the bot will respond "
                "to anyone. Set bridges.matrix.options.allowed_users in config.yaml."
            )
        if not self._admin_users:
            logger.warning(
                "Matrix bridge: admin_users is empty — nobody can use %s in group rooms.",
                self._reset_command,
            )

        self._router.register_platform_handler(Platform.MATRIX.value, self.handle_event)

        client.add_event_callback(self._on_message, RoomMessageText)
        if _HAS_MEDIA_EVENTS:
            for media_cls in (RoomMessageImage, RoomMessageFile, RoomMessageAudio, RoomMessageVideo):
                client.add_event_callback(self._on_media, media_cls)
        else:
            logger.warning(
                "Matrix bridge: media event types not available in this nio version — "
                "file/image attachments will not be received. Upgrade matrix-nio."
            )

        logger.info("Matrix bridge: starting sync loop")
        try:
            await client.sync(timeout=0, full_state=True)
            await client.sync_forever(timeout=self._sync_timeout, full_state=False)
        finally:
            await client.close()
            logger.info("Matrix bridge: client closed")


# ---------------------------------------------------------------------------
# Loader entrypoint (called by main.py)
# ---------------------------------------------------------------------------

async def run(router: "Router") -> None:
    """Entry point called by main.py bridge loader."""
    bridge_cfg = router.config.bridges.get("matrix")
    options: dict = bridge_cfg.options if bridge_cfg else {}
    bridge = MatrixBridge(router, options)
    await bridge.run()

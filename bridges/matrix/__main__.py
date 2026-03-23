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
# GroupBuffer (identical logic to Discord bridge)
# ---------------------------------------------------------------------------

@dataclass
class _BufferedLine:
    user_id: str
    display_name: str
    text: str


class GroupBuffer:
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

        # session_key_str → _ReplyAccumulator
        self._accumulators: dict[str, _ReplyAccumulator] = {}
        # session_key_str → asyncio.Event signalling typing should be active
        self._typing_active: dict[str, asyncio.Event] = {}
        # room_id → GroupBuffer
        self._group_buffers: dict[str, GroupBuffer] = {}
        # sender+room_id → pending Attachments (media arrives before text in Matrix)
        self._pending_attachments: dict[str, list[Attachment]] = {}

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

    def _get_or_create_buffer(self, room_id: str) -> GroupBuffer:
        if room_id not in self._group_buffers:
            self._group_buffers[room_id] = GroupBuffer(self._buffer_timeout_s)
        return self._group_buffers[room_id]

    def _display_name(self, room: MatrixRoom, sender: str) -> str:
        return room.user_name(sender) or sender.split(":")[0].lstrip("@")

    def _is_mentioned(self, body: str) -> bool:
        """Check whether the bot's MXID or local part appears in the message."""
        local = self._username.split(":")[0].lstrip("@")
        return self._username in body or f"@{local}" in body

    def _strip_trigger(self, text: str) -> str:
        """Strip bot mention and command prefix from message text."""
        local = self._username.split(":")[0].lstrip("@")
        text = text.replace(self._username, "").replace(f"@{local}", "")
        if text.startswith(self._prefix):
            text = text[len(self._prefix):]
        return text.strip()

    def _extract_text(
        self, room, event
    ) -> str | None:
        """
        Extract and normalise the user-visible text from a Matrix message.

        Returns the text the agent should receive, or None if the message
        should be silently ignored (group room, trigger required, no trigger).

        DM rooms:   always return the stripped body.
        Group rooms:
          - If the message starts with the command prefix or mentions the bot
            → strip the trigger and return the remainder.
          - Otherwise, if prefix_required is True → return None.
          - Otherwise → return the stripped body.
        """
        body = event.body.strip()

        if self._is_dm_room(room):
            return body

        # Group room
        mentioned = self._is_mentioned(body)
        prefixed = body.startswith(self._prefix)

        if mentioned or prefixed:
            return self._strip_trigger(body)

        if self._prefix_required:
            return None

        return body

    # ------------------------------------------------------------------
    # Event handler registered with Router
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:
        session_key_str = str(event.session_key)
        acc = self._accumulators.get(session_key_str)
        if acc is None:
            logger.debug("Matrix: received event for unknown session %s", session_key_str)
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
            logger.debug("Matrix: tool call %s in session %s", event.tool_name, session_key_str)
        elif isinstance(event, AgentToolResult):
            logger.debug(
                "Matrix: tool result %s (%s) in session %s",
                event.tool_name, "error" if event.is_error else "ok", session_key_str,
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
            session_key = SessionKey.dm(event.sender)
            att_key = f"{event.sender}:{room.room_id}"
            attachments = tuple(self._pending_attachments.pop(att_key, []))

            author = UserIdentity(
                platform=Platform.MATRIX,
                user_id=event.sender,
                username=self._display_name(room, event.sender),
            )
            msg = InboundMessage(
                session_key=session_key,
                author=author,
                content_type=content_type_for(body, bool(attachments)),
                text=body,
                message_id=event.event_id,
                timestamp=time.time(),
                attachments=attachments,
            )
            session_key_str = str(session_key)
            acc = _ReplyAccumulator(self._max_len)
            self._accumulators[session_key_str] = acc
            asyncio.create_task(
                self._handle_turn(msg, room.room_id, session_key_str, acc)
            )
            return

        # ----------------------------------------------------------------
        # Group room path
        # ----------------------------------------------------------------
        session_key = SessionKey.group(Platform.MATRIX, room.room_id)
        buf = self._get_or_create_buffer(room.room_id)

        # /reset — admin only
        if body == self._reset_command:
            if self._is_admin(event.sender):
                buf.clear()
                self._router.reset_session(session_key)
                await self._send(room.room_id, "✅ Session reset.")
                logger.info(
                    "Matrix: group session %s reset by admin %s",
                    room.room_id, event.sender,
                )
            else:
                await self._send(room.room_id, "⛔ Only admins can reset the session.")
            return

        mentioned = self._is_mentioned(body)
        prefixed = body.startswith(self._prefix)
        is_trigger = mentioned or prefixed

        display = self._display_name(room, event.sender)

        if self._prefix_required and not is_trigger:
            # Non-trigger: humanize mentions and buffer.
            humanized = _humanize_matrix_mentions(body, self._own_user_id)

            async def _timeout_flush_cb(
                _buf=buf, _room=room, _session_key=session_key,
            ):
                await self._flush_group_buffer(
                    _buf, _room, _session_key,
                    trigger_user_id=None, trigger_display_name=None, trigger_text=None,
                )

            buf.set_flush_callback(_timeout_flush_cb)
            buf.add(event.sender, display, humanized)
            logger.debug(
                "Matrix: buffered non-trigger message from %s in room %s",
                display, room.room_id,
            )
            return

        # Trigger: strip bot mention/prefix, humanize, collect attachments, flush.
        stripped = self._strip_trigger(body)
        humanized_trigger = _humanize_matrix_mentions(stripped, self._own_user_id)

        att_key = f"{event.sender}:{room.room_id}"
        attachments = tuple(self._pending_attachments.pop(att_key, []))

        await self._flush_group_buffer(
            buf, room, session_key,
            trigger_user_id=event.sender,
            trigger_display_name=display,
            trigger_text=humanized_trigger,
            attachments=attachments,
            trigger_event_id=event.event_id,
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
    # Group flush
    # ------------------------------------------------------------------

    async def _flush_group_buffer(
        self,
        buf: GroupBuffer,
        room: MatrixRoom,
        session_key: SessionKey,
        trigger_user_id: str | None,
        trigger_display_name: str | None,
        trigger_text: str | None,
        attachments: tuple = (),
        trigger_event_id: str | None = None,
    ) -> None:
        lines = buf.flush(
            trigger_user_id=trigger_user_id,
            trigger_display_name=trigger_display_name,
            trigger_text=trigger_text,
        )
        if not lines and not attachments:
            return

        combined_text = _format_buffer(lines)

        if trigger_user_id:
            author_uid = trigger_user_id
            author_name = trigger_display_name or trigger_user_id
            msg_id = trigger_event_id or str(time.time_ns())
        else:
            first = lines[0] if lines else None
            author_uid = first.user_id if first else "unknown"
            author_name = first.display_name if first else "unknown"
            msg_id = str(time.time_ns())

        author = UserIdentity(
            platform=Platform.MATRIX,
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
        acc = _ReplyAccumulator(self._max_len)
        self._accumulators[session_key_str] = acc
        asyncio.create_task(
            self._handle_turn(msg, room.room_id, session_key_str, acc)
        )

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

    async def _handle_turn(
        self,
        msg: InboundMessage,
        room_id: str,
        session_key_str: str,
        acc: _ReplyAccumulator,
    ) -> None:
        done_event = asyncio.Event()
        typing_ev = asyncio.Event()
        self._typing_active[session_key_str] = typing_ev

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
        except Exception:
            logger.exception("Matrix: error handling turn for session %s", session_key_str)
        finally:
            done_event.set()
            self._accumulators.pop(session_key_str, None)
            self._typing_active.pop(session_key_str, None)

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

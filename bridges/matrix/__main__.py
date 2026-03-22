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
                    Messages from any user not on this list are silently
                    ignored before being pushed to the router.
                    Default: []  (WARNING: open access — set this!)
  dm_enabled:       Respond to 1-on-1 rooms. Default: true
  room_ids:         Whitelist of room IDs to respond in. Empty = all rooms
                    the bot is joined to. Default: []
  prefix_required:  In non-DM rooms, only respond when @mentioned or when
                    the message starts with command_prefix. Default: true
  command_prefix:   Text prefix that triggers the bot in rooms.
                    Default: "!"
  max_reply_length: Max characters per Matrix message before chunking.
                    Default: 16000  (Matrix has no hard 2000-char limit)
  sync_timeout_ms:  Long-poll timeout per /sync call in ms. Default: 30000

Password setup:
  export MATRIX_PASSWORD=your-password-here

Required:
  pip install matrix-nio
  For E2EE support: pip install matrix-nio[e2e]

Finding your Matrix user ID:
  Your full MXID is shown in your Matrix client under Settings → Profile,
  in the format @username:homeserver.tld
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginResponse,
    MatrixRoom,
    RoomMessageText,
    SyncError,
)

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
    "password_env": "MATRIX_PASSWORD",
    "device_name": "TinyCTX",
    "store_path": "matrix_store",
    "allowed_users": [],
    "dm_enabled": True,
    "room_ids": [],
    "prefix_required": True,
    "command_prefix": "!",
    "max_reply_length": 16000,
    "sync_timeout_ms": 30000,
}


# ---------------------------------------------------------------------------
# Reply accumulator (same pattern as Discord bridge)
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
        """Wait for the turn to complete and return chunks to send."""
        await self._done.wait()
        if self._error:
            return [f"⚠️ {self._error}"]
        text = "".join(self._buf).strip()
        if not text:
            return []
        # Chunk at max_reply_length.
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
        self._dm_enabled: bool = bool(self._opts["dm_enabled"])
        self._room_ids: set[str] = set(self._opts["room_ids"])
        self._sync_timeout: int = int(self._opts["sync_timeout_ms"])

        # allowed_users: empty set = open access (warn at startup)
        raw_allowed: list = self._opts["allowed_users"]
        self._allowed_users: set[str] = {str(u) for u in raw_allowed}

        # Resolve store path against workspace.
        workspace = str(router.config.workspace.path)
        raw_store = str(self._opts["store_path"])
        if os.path.isabs(raw_store):
            self._store_path = raw_store
        else:
            self._store_path = os.path.join(workspace, raw_store)
        os.makedirs(self._store_path, exist_ok=True)

        # session_key_str → _ReplyAccumulator for the active turn
        self._accumulators: dict[str, _ReplyAccumulator] = {}

        # Populated in run() after login
        self._client: AsyncClient | None = None
        self._own_user_id: str = ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_allowed(self, sender: str) -> bool:
        """Return True if this MXID is permitted to interact with the bot.
        An empty allowlist means open access (with a startup warning)."""
        if not self._allowed_users:
            return True
        return sender in self._allowed_users

    def _is_dm_room(self, room: MatrixRoom) -> bool:
        """Heuristic: a DM room has exactly 2 members."""
        return room.member_count == 2

    def _extract_text(self, room: MatrixRoom, event: RoomMessageText) -> str | None:
        """
        Strip @mention and command prefix from a message body.
        Returns None if prefix_required and no trigger is present.
        """
        is_dm = self._is_dm_room(room)
        body = event.body.strip()

        if is_dm:
            return body  # always respond in DMs

        # Group room — check trigger.
        mention = f"@{self._username.split(':')[0].lstrip('@')}"
        full_mention = self._username  # e.g. @bot:matrix.org

        mentioned = mention in body or full_mention in body
        prefixed = body.startswith(self._prefix)

        if self._prefix_required and not mentioned and not prefixed:
            return None

        # Strip mention and prefix.
        text = body
        text = text.replace(full_mention, "").replace(mention, "")
        if text.startswith(self._prefix):
            text = text[len(self._prefix):]
        return text.strip()

    # ------------------------------------------------------------------
    # Event handler registered with Router
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:
        session_key_str = str(event.session_key)
        acc = self._accumulators.get(session_key_str)
        if acc is None:
            logger.debug("Matrix: received event for unknown session %s", session_key_str)
            return

        if isinstance(event, AgentTextChunk):
            acc.feed(event.text)
        elif isinstance(event, AgentTextFinal):
            acc.finish(event.text)
        elif isinstance(event, AgentToolCall):
            logger.debug("Matrix: tool call %s in session %s", event.tool_name, session_key_str)
        elif isinstance(event, AgentToolResult):
            logger.debug(
                "Matrix: tool result %s (%s) in session %s",
                event.tool_name,
                "error" if event.is_error else "ok",
                session_key_str,
            )
        elif isinstance(event, AgentError):
            acc.error(event.message)

    # ------------------------------------------------------------------
    # nio message callback
    # ------------------------------------------------------------------

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        # Ignore own messages.
        if event.sender == self._own_user_id:
            return

        # Ignore messages older than startup (nio replays history on first sync).
        age_ms = getattr(event, "server_timestamp", 0)
        now_ms = int(time.time() * 1000)
        if age_ms and (now_ms - age_ms) > 60_000:
            return

        # Access control — drop silently if sender not on allowlist.
        if not self._is_allowed(event.sender):
            logger.debug(
                "Matrix: ignoring message from unauthorized user %s", event.sender
            )
            return

        is_dm = self._is_dm_room(room)

        if is_dm and not self._dm_enabled:
            return

        if self._room_ids and room.room_id not in self._room_ids:
            return

        text = self._extract_text(room, event)
        if text is None or not text:
            return

        if is_dm:
            session_key = SessionKey.dm(event.sender)
        else:
            session_key = SessionKey.group(Platform.MATRIX, room.room_id)

        author = UserIdentity(
            platform=Platform.MATRIX,
            user_id=event.sender,
            username=room.user_name(event.sender) or event.sender,
        )
        msg = InboundMessage(
            session_key=session_key,
            author=author,
            content_type=ContentType.TEXT,
            text=text,
            message_id=event.event_id,
            timestamp=time.time(),
        )

        session_key_str = str(session_key)
        acc = _ReplyAccumulator(self._max_len)
        self._accumulators[session_key_str] = acc

        asyncio.create_task(
            self._handle_turn(msg, room.room_id, session_key_str, acc)
        )

    async def _handle_turn(
        self,
        msg: InboundMessage,
        room_id: str,
        session_key_str: str,
        acc: _ReplyAccumulator,
    ) -> None:
        try:
            accepted = await self._router.push(msg)
            if not accepted:
                await self._send(room_id, "⏳ I'm busy — please try again in a moment.")
                return

            chunks = await acc.wait()
            for chunk in chunks:
                await self._send(room_id, chunk)
        except Exception:
            logger.exception("Matrix: error handling turn for session %s", session_key_str)
        finally:
            self._accumulators.pop(session_key_str, None)

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
            encryption_enabled=False,  # flip to True if matrix-nio[e2e] installed
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
        else:
            logger.info(
                "Matrix bridge: %d allowed user(s) configured.", len(self._allowed_users)
            )

        self._router.register_platform_handler(Platform.MATRIX.value, self.handle_event)

        # Register the nio callback.
        client.add_event_callback(self._on_message, RoomMessageText)

        logger.info("Matrix bridge: starting sync loop")
        try:
            # Perform one initial sync to load room state before processing messages.
            await client.sync(timeout=0, full_state=True)
            # Continuous sync loop.
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

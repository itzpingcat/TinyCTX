"""
tests/test_bridges.py

Tests for Discord and Matrix bridge access-control logic, option parsing,
reply accumulation, and message routing.

Phase 2 tree refactor: InboundMessage uses tail_node_id (str) — no SessionKey.
Bridges maintain a _cursors dict mapping cursor_key → node_id.

No real network connections are made. discord.py and matrix-nio are not
imported — the bridge modules are loaded with their external deps stubbed
out so the tests run in any environment.

Run with:
    pytest tests/test_bridges.py -v
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from contracts import (
    AgentError,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    ContentType,
    InboundMessage,
    Platform,
    UserIdentity,
)


# ---------------------------------------------------------------------------
# Stubs for discord.py and matrix-nio
# ---------------------------------------------------------------------------

def _stub_discord():
    discord = types.ModuleType("discord")

    class Intents:
        @staticmethod
        def default():
            obj = MagicMock()
            obj.message_content = False
            obj.members = False
            return obj

    class Client:
        def __init__(self, *, intents=None):
            self._handlers = {}
            self.user = MagicMock()
            self.user.id = 999
            self.user.mentions = []

        def event(self, fn):
            self._handlers[fn.__name__] = fn
            return fn

        async def start(self, token):
            pass

    class DMChannel:
        pass

    class TextChannel:
        pass

    class Thread:
        pass

    discord.Intents     = Intents
    discord.Client      = Client
    discord.DMChannel   = DMChannel
    discord.TextChannel = TextChannel
    discord.Thread      = Thread
    discord.abc         = types.ModuleType("discord.abc")

    class Messageable:
        pass

    discord.abc.Messageable = Messageable
    sys.modules["discord"]         = discord
    sys.modules["discord.abc"]     = discord.abc
    return discord


def _stub_nio():
    nio = types.ModuleType("nio")

    class AsyncClient:
        def __init__(self, *, homeserver="", user="", store_path="", config=None):
            self.user_id = user

        async def login(self, *, password="", device_name=""):
            return LoginResponse()

        def add_event_callback(self, fn, event_type):
            pass

        async def sync(self, *, timeout=0, full_state=False):
            pass

        async def sync_forever(self, *, timeout=30000, full_state=False):
            pass

        async def room_send(self, *, room_id, message_type, content):
            pass

        async def close(self):
            pass

    class AsyncClientConfig:
        def __init__(self, *, store_sync_tokens=True, encryption_enabled=False):
            pass

    class LoginResponse:
        pass

    class MatrixRoom:
        def __init__(self, room_id="!room:server", member_count=2):
            self.room_id = room_id
            self.member_count = member_count

        def user_name(self, user_id):
            return user_id.split(":")[0].lstrip("@")

    class RoomMessageText:
        def __init__(self, sender, body, event_id="$evt1"):
            self.sender = sender
            self.body = body
            self.event_id = event_id
            self.server_timestamp = int(time.time() * 1000)
            self.source = {}

    class RoomMessageMedia:
        def __init__(self, sender="@user:server", body="file", url="mxc://server/abc", event_id="$media1"):
            self.sender = sender
            self.body = body
            self.url = url
            self.event_id = event_id
            self.server_timestamp = int(time.time() * 1000)
            self.source = {"content": {"body": body}}
            self.info = {"mimetype": "application/octet-stream"}

    class RoomMessageImage(RoomMessageMedia):
        pass

    class RoomMessageFile(RoomMessageMedia):
        pass

    class RoomMessageAudio(RoomMessageMedia):
        pass

    class RoomMessageVideo(RoomMessageMedia):
        pass

    class SyncError:
        pass

    nio.AsyncClient        = AsyncClient
    nio.AsyncClientConfig  = AsyncClientConfig
    nio.LoginResponse      = LoginResponse
    nio.MatrixRoom         = MatrixRoom
    nio.RoomMessageText    = RoomMessageText
    nio.RoomMessageMedia   = RoomMessageMedia
    nio.RoomMessageImage   = RoomMessageImage
    nio.RoomMessageFile    = RoomMessageFile
    nio.RoomMessageAudio   = RoomMessageAudio
    nio.RoomMessageVideo   = RoomMessageVideo
    nio.SyncError          = SyncError
    sys.modules["nio"] = nio
    return nio


_stub_discord()
_stub_nio()

from bridges.discord.__main__ import DiscordBridge, _ReplyAccumulator as DiscordAccumulator
from bridges.matrix.__main__ import MatrixBridge, _ReplyAccumulator as MatrixAccumulator


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_router(workspace="/tmp/tinyctx_test"):
    router = MagicMock()
    router.config.workspace.path = workspace
    router._config.workspace.path = workspace
    router.config.bridges = {}
    router.push = AsyncMock(return_value=True)
    router.register_platform_handler = MagicMock()
    return router


def _trace_id():
    return "trace-test"


def _node_id():
    return str(uuid.uuid4())


def _make_agent_event(cls, node_id, **kwargs):
    return cls(
        tail_node_id=node_id,
        lane_node_id=node_id,
        trace_id=_trace_id(),
        reply_to_message_id="msg-1",
        **kwargs,
    )


# ===========================================================================
# Discord bridge tests
# ===========================================================================

class TestDiscordBridgeOptions:
    def _bridge(self, options=None):
        return DiscordBridge(_make_router(), options or {})

    def test_defaults_applied(self):
        b = self._bridge()
        assert b._dm_enabled is True
        assert b._prefix == "!"
        assert b._prefix_required is True
        assert b._max_len == 1900
        assert b._typing is True
        assert b._allowed_users == set()
        assert b._guild_ids == set()

    def test_allowed_users_parsed_as_ints(self):
        b = self._bridge({"allowed_users": ["123456789", "987654321"]})
        assert 123456789 in b._allowed_users
        assert 987654321 in b._allowed_users

    def test_guild_ids_parsed_as_ints(self):
        b = self._bridge({"guild_ids": [111, 222]})
        assert 111 in b._guild_ids
        assert 222 in b._guild_ids

    def test_options_override_defaults(self):
        b = self._bridge({
            "dm_enabled": False,
            "command_prefix": ">>",
            "max_reply_length": 500,
            "typing_indicator": False,
        })
        assert b._dm_enabled is False
        assert b._prefix == ">>"
        assert b._max_len == 500
        assert b._typing is False


class TestDiscordAllowlist:
    def _bridge(self, allowed=None):
        opts = {"allowed_users": allowed} if allowed is not None else {}
        return DiscordBridge(_make_router(), opts)

    def test_empty_allowlist_permits_everyone(self):
        b = self._bridge(allowed=[])
        assert b._is_allowed(123) is True
        assert b._is_allowed(999999) is True

    def test_allowlist_permits_listed_user(self):
        b = self._bridge(allowed=[111, 222])
        assert b._is_allowed(111) is True
        assert b._is_allowed(222) is True

    def test_allowlist_blocks_unlisted_user(self):
        b = self._bridge(allowed=[111])
        assert b._is_allowed(999) is False

    def test_single_user_allowlist(self):
        b = self._bridge(allowed=[42])
        assert b._is_allowed(42) is True
        assert b._is_allowed(43) is False


class TestDiscordMessageFiltering:
    def _bridge(self, allowed=None):
        router = _make_router()
        opts = {"allowed_users": allowed} if allowed is not None else {}
        b = DiscordBridge(router, opts)
        b._router = router
        # Pre-seed a cursor so _get_or_create_cursor doesn't hit the DB
        b._store.set("dm:42", _node_id())
        b._store.set("dm:111", _node_id())
        b._store.set("dm:99999", _node_id())
        return b, router

    def _make_discord_message(self, author_id: int, content: str, is_dm: bool = True):
        msg = MagicMock()
        msg.author.id = author_id
        msg.author.display_name = f"user_{author_id}"
        msg.content = content
        msg.id = 12345
        msg.attachments = []

        import discord
        if is_dm:
            msg.channel = MagicMock(spec=discord.DMChannel)
        else:
            msg.channel = MagicMock()
            msg.channel.__class__ = MagicMock

        return msg

    @pytest.mark.asyncio
    async def test_unauthorized_user_message_dropped(self):
        b, router = self._bridge(allowed=[111])
        msg = self._make_discord_message(author_id=999, content="hello")
        await b._on_message(msg)
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_authorized_user_message_accepted(self):
        b, router = self._bridge(allowed=[111])
        msg = self._make_discord_message(author_id=111, content="hello")
        await b._on_message(msg)
        await asyncio.sleep(0)
        router.push.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_allowlist_accepts_anyone(self):
        b, router = self._bridge(allowed=[])
        b._store.set("dm:99999", _node_id())
        msg = self._make_discord_message(author_id=99999, content="hello")
        await b._on_message(msg)
        await asyncio.sleep(0)
        router.push.assert_called_once()

    @pytest.mark.asyncio
    async def test_bot_own_message_ignored(self):
        b, router = self._bridge(allowed=[])
        msg = self._make_discord_message(author_id=b._client.user.id, content="echo")
        await b._on_message(msg)
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_text_not_pushed(self):
        b, router = self._bridge(allowed=[])
        b._store.set("dm:42", _node_id())
        msg = self._make_discord_message(author_id=42, content="   ")
        await b._on_message(msg)
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_disabled_drops_dm(self):
        b, router = self._bridge(allowed=[])
        b._dm_enabled = False
        msg = self._make_discord_message(author_id=42, content="hello", is_dm=True)
        await b._on_message(msg)
        router.push.assert_not_called()


class TestDiscordMessageRouting:
    """Test that pushed InboundMessages carry a tail_node_id and correct author."""

    def _bridge(self):
        b = DiscordBridge(_make_router(), {})
        # Pre-seed cursors so no DB access needed
        b._store.set("dm:42", str(uuid.uuid4()))
        b._store.set("dm:77", str(uuid.uuid4()))
        return b

    def _make_discord_dm_message(self, author_id: int):
        import discord
        msg = MagicMock()
        msg.author.id = author_id
        msg.author.display_name = "tester"
        msg.content = "hello"
        msg.id = 1
        msg.attachments = []
        msg.channel = MagicMock(spec=discord.DMChannel)
        return msg

    @pytest.mark.asyncio
    async def test_dm_message_has_tail_node_id(self):
        b = self._bridge()
        pushed = []
        b._router.push = AsyncMock(side_effect=lambda m: pushed.append(m) or True)
        msg = self._make_discord_dm_message(author_id=42)
        await b._on_message(msg)
        await asyncio.sleep(0)
        assert len(pushed) == 1
        assert isinstance(pushed[0].tail_node_id, str)
        assert len(pushed[0].tail_node_id) > 0

    @pytest.mark.asyncio
    async def test_author_identity_populated(self):
        b = self._bridge()
        pushed = []
        b._router.push = AsyncMock(side_effect=lambda m: pushed.append(m) or True)
        msg = self._make_discord_dm_message(author_id=77)
        await b._on_message(msg)
        await asyncio.sleep(0)
        assert pushed[0].author.platform == Platform.DISCORD
        assert pushed[0].author.user_id == "77"


class TestDiscordReplyAccumulator:
    def _channel(self, sent: list):
        ch = AsyncMock()
        ch.send = AsyncMock(side_effect=lambda text: sent.append(text))
        return ch

    @pytest.mark.asyncio
    async def test_streaming_chunks_assembled_and_sent(self):
        sent = []
        acc = DiscordAccumulator(self._channel(sent), max_len=1900)
        acc.feed("hello ")
        acc.feed("world")
        acc.finish("")
        await acc.wait_and_send()
        assert sent == ["hello world"]

    @pytest.mark.asyncio
    async def test_non_streaming_final_text_sent(self):
        sent = []
        acc = DiscordAccumulator(self._channel(sent), max_len=1900)
        acc.finish("full reply")
        await acc.wait_and_send()
        assert sent == ["full reply"]

    @pytest.mark.asyncio
    async def test_long_reply_chunked(self):
        sent = []
        acc = DiscordAccumulator(self._channel(sent), max_len=5)
        acc.finish("abcdefghij")
        await acc.wait_and_send()
        assert sent == ["abcde", "fghij"]

    @pytest.mark.asyncio
    async def test_error_sends_warning(self):
        sent = []
        acc = DiscordAccumulator(self._channel(sent), max_len=1900)
        acc.error("LLM unavailable")
        await acc.wait_and_send()
        assert len(sent) == 1
        assert "LLM unavailable" in sent[0]
        assert "⚠️" in sent[0]

    @pytest.mark.asyncio
    async def test_empty_reply_sends_nothing(self):
        sent = []
        acc = DiscordAccumulator(self._channel(sent), max_len=1900)
        acc.finish("")
        await acc.wait_and_send()
        assert sent == []


class TestDiscordEventHandling:
    def _bridge(self):
        return DiscordBridge(_make_router(), {})

    @pytest.mark.asyncio
    async def test_text_chunk_feeds_accumulator(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentTextChunk, nid, text="hi")
        await b.handle_event(event)
        acc.feed.assert_called_once_with("hi")

    @pytest.mark.asyncio
    async def test_text_final_finishes_accumulator(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentTextFinal, nid, text="done")
        await b.handle_event(event)
        acc.finish.assert_called_once_with("done")

    @pytest.mark.asyncio
    async def test_agent_error_calls_error(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentError, nid, message="boom")
        await b.handle_event(event)
        acc.error.assert_called_once_with("boom")

    @pytest.mark.asyncio
    async def test_tool_call_logged_not_sent(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentToolCall, nid, call_id="c1", tool_name="search", args={})
        await b.handle_event(event)
        acc.feed.assert_not_called()
        acc.finish.assert_not_called()
        acc.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_node_event_ignored(self):
        b = self._bridge()
        event = _make_agent_event(AgentTextChunk, _node_id(), text="nobody home")
        await b.handle_event(event)  # should not raise

    @pytest.mark.asyncio
    async def test_backpressure_sends_busy_message(self):
        b = self._bridge()
        sent = []
        channel = AsyncMock()
        channel.send = AsyncMock(side_effect=lambda t: sent.append(t))
        b._router.push = AsyncMock(return_value=False)

        nid = _node_id()
        msg = InboundMessage(
            tail_node_id=nid,
            author=UserIdentity(Platform.DISCORD, "u1", "alice"),
            content_type=ContentType.TEXT,
            text="hello",
            message_id="m1",
            timestamp=time.time(),
        )
        acc = DiscordAccumulator(channel, max_len=1900)
        b._accumulators[nid] = acc

        await b._handle_turn(msg, channel, nid, acc)
        assert any("busy" in s.lower() or "⏳" in s for s in sent)


# ===========================================================================
# Matrix bridge tests
# ===========================================================================

class TestMatrixBridgeOptions:
    def _bridge(self, options=None):
        opts = {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
            **(options or {}),
        }
        return MatrixBridge(_make_router(), opts)

    def test_defaults_applied(self):
        b = self._bridge()
        assert b._dm_enabled is True
        assert b._prefix == "!"
        assert b._prefix_required is True
        assert b._max_len == 16000
        assert b._allowed_users == set()
        assert b._room_ids == set()

    def test_allowed_users_parsed_as_strings(self):
        b = self._bridge({"allowed_users": ["@alice:matrix.org", "@bob:matrix.org"]})
        assert "@alice:matrix.org" in b._allowed_users
        assert "@bob:matrix.org" in b._allowed_users

    def test_room_ids_parsed(self):
        b = self._bridge({"room_ids": ["!abc:matrix.org", "!def:matrix.org"]})
        assert "!abc:matrix.org" in b._room_ids
        assert "!def:matrix.org" in b._room_ids

    def test_options_override_defaults(self):
        b = self._bridge({
            "dm_enabled": False,
            "command_prefix": "//",
            "max_reply_length": 500,
            "prefix_required": False,
        })
        assert b._dm_enabled is False
        assert b._prefix == "//"
        assert b._max_len == 500
        assert b._prefix_required is False

    def test_store_path_resolves_relative_to_workspace(self, tmp_path):
        router = _make_router(workspace=str(tmp_path))
        b = MatrixBridge(router, {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
            "store_path": "matrix_store",
        })
        assert str(tmp_path) in b._store_path
        assert "matrix_store" in b._store_path

    def test_absolute_store_path_not_modified(self, tmp_path):
        router = _make_router(workspace=str(tmp_path))
        abs_path = str(tmp_path / "absolute_store")
        b = MatrixBridge(router, {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
            "store_path": abs_path,
        })
        assert b._store_path == abs_path


class TestMatrixAllowlist:
    def _bridge(self, allowed=None):
        opts = {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
        }
        if allowed is not None:
            opts["allowed_users"] = allowed
        return MatrixBridge(_make_router(), opts)

    def test_empty_allowlist_permits_everyone(self):
        b = self._bridge(allowed=[])
        assert b._is_allowed("@anyone:matrix.org") is True

    def test_allowlist_permits_listed_mxid(self):
        b = self._bridge(allowed=["@alice:matrix.org"])
        assert b._is_allowed("@alice:matrix.org") is True

    def test_allowlist_blocks_unlisted_mxid(self):
        b = self._bridge(allowed=["@alice:matrix.org"])
        assert b._is_allowed("@eve:matrix.org") is False

    def test_mxid_case_sensitive(self):
        b = self._bridge(allowed=["@Alice:matrix.org"])
        assert b._is_allowed("@alice:matrix.org") is False
        assert b._is_allowed("@Alice:matrix.org") is True


class TestMatrixDMDetection:
    def _bridge(self):
        return MatrixBridge(_make_router(), {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
        })

    def test_two_member_room_is_dm(self):
        import nio
        b = self._bridge()
        room = nio.MatrixRoom(member_count=2)
        assert b._is_dm_room(room) is True

    def test_three_member_room_is_not_dm(self):
        import nio
        b = self._bridge()
        room = nio.MatrixRoom(member_count=3)
        assert b._is_dm_room(room) is False

    def test_one_member_room_is_not_dm(self):
        import nio
        b = self._bridge()
        room = nio.MatrixRoom(member_count=1)
        assert b._is_dm_room(room) is False


class TestMatrixMessageFiltering:
    def _bridge(self, allowed=None):
        opts = {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
        }
        if allowed is not None:
            opts["allowed_users"] = allowed
        router = _make_router()
        b = MatrixBridge(router, opts)
        b._own_user_id = "@bot:matrix.org"
        b._router = router
        # Pre-seed cursors
        b._cursors["dm:@alice:matrix.org"] = _node_id()
        b._cursors["dm:@user:matrix.org"] = _node_id()
        return b, router

    def _dm_room(self):
        import nio
        return nio.MatrixRoom(member_count=2)

    def _event(self, sender, body, fresh=True):
        import nio
        evt = nio.RoomMessageText(sender=sender, body=body)
        if not fresh:
            evt.server_timestamp = int((time.time() - 120) * 1000)
        return evt

    @pytest.mark.asyncio
    async def test_unauthorized_sender_dropped(self):
        b, router = self._bridge(allowed=["@alice:matrix.org"])
        await b._on_message(self._dm_room(), self._event("@eve:matrix.org", "hello"))
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_authorized_sender_accepted(self):
        b, router = self._bridge(allowed=["@alice:matrix.org"])
        await b._on_message(self._dm_room(), self._event("@alice:matrix.org", "hello"))
        await asyncio.sleep(0)
        router.push.assert_called_once()

    @pytest.mark.asyncio
    async def test_own_message_ignored(self):
        b, router = self._bridge(allowed=[])
        await b._on_message(self._dm_room(), self._event("@bot:matrix.org", "echo"))
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_old_message_ignored(self):
        b, router = self._bridge(allowed=[])
        await b._on_message(self._dm_room(), self._event("@user:matrix.org", "old msg", fresh=False))
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_disabled_drops_dm(self):
        b, router = self._bridge(allowed=[])
        b._dm_enabled = False
        await b._on_message(self._dm_room(), self._event("@user:matrix.org", "hello"))
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_room_whitelist_filters_other_rooms(self):
        import nio
        b, router = self._bridge(allowed=[])
        b._room_ids = {"!allowed:matrix.org"}
        other_room = nio.MatrixRoom(room_id="!other:matrix.org", member_count=2)
        await b._on_message(other_room, self._event("@user:matrix.org", "hello"))
        router.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_room_whitelist_permits_listed_room(self):
        import nio
        b, router = self._bridge(allowed=[])
        b._room_ids = {"!allowed:matrix.org"}
        b._cursors["dm:@user:matrix.org"] = _node_id()
        allowed_room = nio.MatrixRoom(room_id="!allowed:matrix.org", member_count=2)
        await b._on_message(allowed_room, self._event("@user:matrix.org", "hello"))
        await asyncio.sleep(0)
        router.push.assert_called_once()


class TestMatrixMessageRouting:
    """Test that pushed InboundMessages have correct tail_node_id and author."""

    def _bridge(self):
        b = MatrixBridge(_make_router(), {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
        })
        b._own_user_id = "@bot:matrix.org"
        # Pre-seed cursors
        b._cursors["dm:@user:matrix.org"] = _node_id()
        b._cursors["dm:@alice:matrix.org"] = _node_id()
        b._cursors["group:!room:matrix.org"] = _node_id()
        return b

    def _dm_room(self):
        import nio
        return nio.MatrixRoom(member_count=2)

    def _group_room(self, room_id="!room:matrix.org"):
        import nio
        return nio.MatrixRoom(room_id=room_id, member_count=10)

    def _event(self, sender="@user:matrix.org", body="hello"):
        import nio
        return nio.RoomMessageText(sender=sender, body=body)

    @pytest.mark.asyncio
    async def test_dm_message_has_tail_node_id(self):
        b = self._bridge()
        pushed = []
        b._router.push = AsyncMock(side_effect=lambda m: pushed.append(m) or True)
        await b._on_message(self._dm_room(), self._event())
        await asyncio.sleep(0)
        assert len(pushed) == 1
        assert isinstance(pushed[0].tail_node_id, str)
        assert len(pushed[0].tail_node_id) > 0

    @pytest.mark.asyncio
    async def test_group_message_has_tail_node_id(self):
        b = self._bridge()
        b._prefix_required = False
        pushed = []
        b._router.push = AsyncMock(side_effect=lambda m: pushed.append(m) or True)
        await b._on_message(self._group_room("!room:matrix.org"), self._event())
        await asyncio.sleep(0)
        assert len(pushed) == 1
        assert isinstance(pushed[0].tail_node_id, str)

    @pytest.mark.asyncio
    async def test_author_identity_populated(self):
        b = self._bridge()
        pushed = []
        b._router.push = AsyncMock(side_effect=lambda m: pushed.append(m) or True)
        await b._on_message(self._dm_room(), self._event(sender="@alice:matrix.org"))
        await asyncio.sleep(0)
        assert pushed[0].author.platform == Platform.MATRIX
        assert pushed[0].author.user_id == "@alice:matrix.org"


class TestMatrixReplyAccumulator:
    @pytest.mark.asyncio
    async def test_streaming_chunks_assembled(self):
        acc = MatrixAccumulator(max_len=16000)
        acc.feed("hello ")
        acc.feed("world")
        acc.finish("")
        chunks = await acc.wait()
        assert chunks == ["hello world"]

    @pytest.mark.asyncio
    async def test_non_streaming_final_text(self):
        acc = MatrixAccumulator(max_len=16000)
        acc.finish("full reply")
        chunks = await acc.wait()
        assert chunks == ["full reply"]

    @pytest.mark.asyncio
    async def test_long_reply_chunked(self):
        acc = MatrixAccumulator(max_len=5)
        acc.finish("abcdefghij")
        chunks = await acc.wait()
        assert chunks == ["abcde", "fghij"]

    @pytest.mark.asyncio
    async def test_error_returns_warning_chunk(self):
        acc = MatrixAccumulator(max_len=16000)
        acc.error("server error")
        chunks = await acc.wait()
        assert len(chunks) == 1
        assert "server error" in chunks[0]
        assert "⚠️" in chunks[0]

    @pytest.mark.asyncio
    async def test_empty_reply_returns_empty_list(self):
        acc = MatrixAccumulator(max_len=16000)
        acc.finish("")
        chunks = await acc.wait()
        assert chunks == []


class TestMatrixEventHandling:
    def _bridge(self):
        b = MatrixBridge(_make_router(), {
            "homeserver": "https://matrix.org",
            "username": "@bot:matrix.org",
        })
        b._own_user_id = "@bot:matrix.org"
        return b

    @pytest.mark.asyncio
    async def test_text_chunk_feeds_accumulator(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentTextChunk, nid, text="hi")
        await b.handle_event(event)
        acc.feed.assert_called_once_with("hi")

    @pytest.mark.asyncio
    async def test_text_final_finishes_accumulator(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentTextFinal, nid, text="done")
        await b.handle_event(event)
        acc.finish.assert_called_once_with("done")

    @pytest.mark.asyncio
    async def test_agent_error_calls_error(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentError, nid, message="boom")
        await b.handle_event(event)
        acc.error.assert_called_once_with("boom")

    @pytest.mark.asyncio
    async def test_tool_call_does_not_touch_accumulator(self):
        b = self._bridge()
        nid = _node_id()
        acc = MagicMock()
        b._accumulators[nid] = acc

        event = _make_agent_event(AgentToolCall, nid, call_id="c1", tool_name="fn", args={})
        await b.handle_event(event)
        acc.feed.assert_not_called()
        acc.finish.assert_not_called()
        acc.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_node_ignored(self):
        b = self._bridge()
        event = _make_agent_event(AgentTextChunk, _node_id(), text="nobody home")
        await b.handle_event(event)  # should not raise

    @pytest.mark.asyncio
    async def test_backpressure_sends_busy_message(self):
        b = self._bridge()
        b._client = AsyncMock()
        b._router.push = AsyncMock(return_value=False)

        nid = _node_id()
        msg = InboundMessage(
            tail_node_id=nid,
            author=UserIdentity(Platform.MATRIX, "@user:matrix.org", "user"),
            content_type=ContentType.TEXT,
            text="hello",
            message_id="m1",
            timestamp=time.time(),
        )
        acc = MatrixAccumulator(max_len=16000)
        b._accumulators[nid] = acc

        await b._handle_turn(msg, "!room:matrix.org", nid, acc)
        b._client.room_send.assert_called_once()
        call_kwargs = b._client.room_send.call_args.kwargs
        body = call_kwargs["content"]["body"]
        assert "busy" in body.lower() or "⏳" in body

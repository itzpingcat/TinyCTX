"""
tests/test_gateway.py

Tests for Router, _LaneRouter, and Lane.

All routing is keyed by tail_node_id (str) — SessionKey and ChatType are gone.

We avoid starting real AgentLoops by monkey-patching AgentLoop so the
router's routing and dispatch logic can be tested in pure isolation.

Run with:
    python -m pytest tests/test_gateway.py -v
"""
import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from TinyCTX.contracts import (
    UserIdentity, InboundMessage,
    AgentTextFinal, AgentError,
    Platform, ContentType,
)
from TinyCTX.router import Router, Lane, _LaneRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_id():
    return str(uuid.uuid4())


def _make_config():
    cfg = MagicMock()
    cfg.models = {"primary": MagicMock()}
    cfg.llm.primary = "primary"
    cfg.llm.fallback = []
    cfg.context = 4096
    cfg.max_tool_cycles = 5
    cfg.workspace.path = "/tmp/tinyctx_test"
    return cfg


def _make_user(platform=Platform.CLI, user_id="u1", username="alice"):
    return UserIdentity(platform=platform, user_id=user_id, username=username)


def _make_msg(node_id, text="hello", user_id="u1", platform=Platform.CLI):
    return InboundMessage(
        tail_node_id=node_id,
        author=_make_user(platform=platform, user_id=user_id),
        content_type=ContentType.TEXT,
        text=text,
        message_id="msg-1",
        timestamp=0.0,
    )


def _make_final(node_id, text="hi"):
    return AgentTextFinal(
        tail_node_id=node_id,
        text=text,
        reply_to_message_id="msg-1",
        trace_id="trace-1",
    )


# ---------------------------------------------------------------------------
# Fixture: a Router with AgentLoop stubbed out
# ---------------------------------------------------------------------------

@pytest.fixture
def gw():
    cfg = _make_config()

    async def _noop_run(msg, abort_event=None):
        return
        yield

    with patch("router.AgentLoop") as MockLoop:
        instance = MagicMock()
        instance.run = _noop_run
        instance.gateway = None
        instance.reset = MagicMock()
        instance._tail_node_id = "node-stub"
        MockLoop.return_value = instance
        router = Router(config=cfg)
        router._mock_loop_instance = instance
        yield router


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

class TestHandlerRegistration:
    def test_register_platform_handler(self, gw):
        handler = AsyncMock()
        gw.register_platform_handler("cli", handler)
        assert "cli" in gw._platform_handlers

    def test_register_reply_handler_alias(self, gw):
        handler = AsyncMock()
        gw.register_reply_handler("cli", handler)
        assert "cli" in gw._platform_handlers

    def test_register_cursor_handler(self, gw):
        nid = _node_id()
        handler = AsyncMock()
        gw.register_cursor_handler(nid, handler)
        assert nid in gw._cursor_handlers

    def test_unregister_cursor_handler(self, gw):
        nid = _node_id()
        handler = AsyncMock()
        gw.register_cursor_handler(nid, handler)
        gw.unregister_cursor_handler(nid)
        assert nid not in gw._cursor_handlers

    def test_unregister_nonexistent_is_noop(self, gw):
        gw.unregister_cursor_handler("nonexistent-node-id")  # should not raise

    def test_register_multiple_platforms(self, gw):
        gw.register_platform_handler("cli", AsyncMock())
        gw.register_platform_handler("discord", AsyncMock())
        assert "cli" in gw._platform_handlers
        assert "discord" in gw._platform_handlers


# ---------------------------------------------------------------------------
# Lane creation / routing
# ---------------------------------------------------------------------------

class TestLaneRouting:
    @pytest.mark.asyncio
    async def test_message_creates_lane(self, gw):
        nid = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid))
        await asyncio.sleep(0)
        assert nid in gw._lane_router._lanes

    @pytest.mark.asyncio
    async def test_same_node_id_reuses_lane(self, gw):
        nid = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid, text="first"))
        await gw.push(_make_msg(nid, text="second"))
        await asyncio.sleep(0)
        assert len(gw._lane_router._lanes) == 1

    @pytest.mark.asyncio
    async def test_different_node_ids_get_different_lanes(self, gw):
        nid1 = _node_id()
        nid2 = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid1))
        await gw.push(_make_msg(nid2))
        await asyncio.sleep(0)
        assert len(gw._lane_router._lanes) == 2

    @pytest.mark.asyncio
    async def test_active_lanes_reflects_open_lanes(self, gw):
        nid1 = _node_id()
        nid2 = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid1))
        await gw.push(_make_msg(nid2))
        await asyncio.sleep(0)
        assert set(gw.active_lanes) == {nid1, nid2}


# ---------------------------------------------------------------------------
# Platform tracking for event dispatch
# ---------------------------------------------------------------------------

class TestPlatformTracking:
    @pytest.mark.asyncio
    async def test_platform_recorded_on_push(self, gw):
        nid = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid, platform=Platform.CLI))
        assert gw._node_platforms.get(nid) == "cli"

    @pytest.mark.asyncio
    async def test_platform_updated_on_second_push(self, gw):
        nid = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        gw.register_platform_handler("discord", AsyncMock())
        await gw.push(_make_msg(nid, platform=Platform.CLI))
        await gw.push(InboundMessage(
            tail_node_id=nid,
            author=UserIdentity(platform=Platform.DISCORD, user_id="u1", username="alice"),
            content_type=ContentType.TEXT,
            text="hello from discord",
            message_id="msg-2",
            timestamp=1.0,
        ))
        assert gw._node_platforms.get(nid) == "discord"


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------

class TestEventDispatch:
    @pytest.mark.asyncio
    async def test_event_dispatched_to_platform_handler(self, gw):
        nid = _node_id()
        received = []

        async def handler(event):
            received.append(event)

        gw.register_platform_handler("cli", handler)
        gw._node_platforms[nid] = "cli"

        await gw._dispatch_event(_make_final(nid))
        assert received[0].tail_node_id == nid

    @pytest.mark.asyncio
    async def test_cursor_handler_takes_priority(self, gw):
        nid = _node_id()
        platform_received = []
        cursor_received = []

        async def platform_handler(event):
            platform_received.append(event)

        async def cursor_handler(event):
            cursor_received.append(event)

        gw.register_platform_handler("cli", platform_handler)
        gw._node_platforms[nid] = "cli"
        gw.register_cursor_handler(nid, cursor_handler)

        await gw._dispatch_event(_make_final(nid))
        assert cursor_received == [_make_final(nid)]
        assert platform_received == []

    @pytest.mark.asyncio
    async def test_event_dropped_if_no_platform_known(self, gw, caplog):
        nid = _node_id()
        await gw._dispatch_event(_make_final(nid))
        assert "Cannot determine platform" in caplog.text

    @pytest.mark.asyncio
    async def test_event_dropped_if_no_handler_registered(self, gw, caplog):
        nid = _node_id()
        gw._node_platforms[nid] = "matrix"
        await gw._dispatch_event(_make_final(nid))
        assert "No handler" in caplog.text

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate(self, gw):
        nid = _node_id()
        gw._node_platforms[nid] = "cli"

        async def bad_handler(event):
            raise RuntimeError("handler blew up")

        gw.register_platform_handler("cli", bad_handler)
        await gw._dispatch_event(_make_final(nid))  # should not raise


# ---------------------------------------------------------------------------
# reset_lane
# ---------------------------------------------------------------------------

class TestResetLane:
    @pytest.mark.asyncio
    async def test_reset_existing_lane_calls_loop_reset(self, gw):
        nid = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid))
        await asyncio.sleep(0)

        gw.reset_lane(nid)
        gw._mock_loop_instance.reset.assert_called_once()

    def test_reset_nonexistent_lane_is_noop(self, gw):
        gw.reset_lane("nonexistent-node-id")  # should not raise


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_closes_all_lanes(self, gw):
        nid1 = _node_id()
        nid2 = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid1))
        await gw.push(_make_msg(nid2))
        await asyncio.sleep(0)

        await gw.shutdown()
        assert gw._lane_router._lanes == {}

    @pytest.mark.asyncio
    async def test_active_lanes_empty_after_shutdown(self, gw):
        nid = _node_id()
        gw.register_platform_handler("cli", AsyncMock())
        await gw.push(_make_msg(nid))
        await asyncio.sleep(0)

        await gw.shutdown()
        assert gw.active_lanes == []

"""
tests/test_router.py

Tests for GroupLane, GroupPolicy, _gp_strip_trigger, and
Router.set_group_activation.

Routing is keyed by tail_node_id (str cursor) — SessionKey is gone.

Run with:
    python -m pytest tests/test_router.py -v
"""
from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from TinyCTX.contracts import (
    ActivationMode,
    ContentType,
    GroupPolicy,
    InboundMessage,
    Platform,
    UserIdentity,
)
from TinyCTX.router import GroupLane, Lane, Router, _gp_strip_trigger, _gp_replace_text


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


def _make_user(username="alice", user_id="@alice:matrix.org"):
    return UserIdentity(platform=Platform.MATRIX, user_id=user_id, username=username)


def _policy(
    activation=ActivationMode.MENTION,
    prefix="!",
    bot_mxid="@bot:matrix.org",
    bot_localpart="bot",
    buffer_timeout_s=0.0,
):
    return GroupPolicy(
        activation=activation,
        trigger_prefix=prefix,
        bot_mxid=bot_mxid,
        bot_localpart=bot_localpart,
        buffer_timeout_s=buffer_timeout_s,
    )


def _msg(text, node_id=None, policy=None, username="alice", user_id="@alice:matrix.org"):
    nid = node_id or _node_id()
    return InboundMessage(
        tail_node_id=nid,
        author=_make_user(username=username, user_id=user_id),
        content_type=ContentType.TEXT,
        text=text,
        message_id="msg-1",
        timestamp=time.time(),
        group_policy=policy,
    )


def _make_lane(policy=None):
    """Create a GroupLane with a mocked inner Lane."""
    inner = MagicMock(spec=Lane)
    inner.enqueue = AsyncMock(return_value=True)
    inner.node_id = _node_id()
    inner.queue = MagicMock()
    inner.loop = MagicMock()
    gl = GroupLane(inner, policy or _policy())
    return gl, inner


# ---------------------------------------------------------------------------
# _gp_strip_trigger
# ---------------------------------------------------------------------------

class TestStripTrigger:
    def test_strips_full_mxid(self):
        p = _policy(bot_mxid="@bot:matrix.org", bot_localpart="bot")
        result = _gp_strip_trigger("@bot:matrix.org do the thing", p)
        assert result == "do the thing"

    def test_strips_localpart(self):
        p = _policy(bot_mxid="@bot:matrix.org", bot_localpart="bot")
        result = _gp_strip_trigger("@bot do the thing", p)
        assert result == "do the thing"

    def test_strips_prefix(self):
        p = _policy(prefix="!")
        result = _gp_strip_trigger("!hello world", p)
        assert result == "hello world"

    def test_no_trigger_passthrough(self):
        p = _policy()
        result = _gp_strip_trigger("just chatting", p)
        assert result == "just chatting"

    def test_strips_whitespace(self):
        p = _policy(prefix="!")
        result = _gp_strip_trigger("!  spaced out  ", p)
        assert result == "spaced out"

    def test_empty_bot_mxid_skipped(self):
        p = GroupPolicy(activation=ActivationMode.MENTION, trigger_prefix="!", bot_mxid="", bot_localpart="")
        result = _gp_strip_trigger("!hello", p)
        assert result == "hello"


# ---------------------------------------------------------------------------
# GroupLane — trigger detection
# ---------------------------------------------------------------------------

class TestGroupLaneTriggerDetection:
    def test_mention_mode_prefix_triggers(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.MENTION, prefix="!"))
        assert gl._is_trigger("!hello", gl._policy) is True

    def test_mention_mode_full_mxid_triggers(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.MENTION))
        assert gl._is_trigger("@bot:matrix.org do something", gl._policy) is True

    def test_mention_mode_localpart_triggers(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.MENTION))
        assert gl._is_trigger("@bot do something", gl._policy) is True

    def test_mention_mode_plain_message_not_trigger(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.MENTION))
        assert gl._is_trigger("just chatting", gl._policy) is False

    def test_prefix_mode_prefix_triggers(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.PREFIX, prefix="!"))
        assert gl._is_trigger("!hello", gl._policy) is True

    def test_prefix_mode_mention_does_not_trigger(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.PREFIX, prefix="!"))
        assert gl._is_trigger("@bot:matrix.org hello", gl._policy) is False

    def test_prefix_mode_plain_not_trigger(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.PREFIX, prefix="!"))
        assert gl._is_trigger("just chatting", gl._policy) is False


# ---------------------------------------------------------------------------
# GroupLane — push() routing
# ---------------------------------------------------------------------------

class TestGroupLanePush:
    @pytest.mark.asyncio
    async def test_trigger_message_forwarded(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION, prefix="!"))
        await gl.push(_msg("!hello there"))
        inner.enqueue.assert_called_once()
        forwarded = inner.enqueue.call_args[0][0]
        assert forwarded.text == "hello there"

    @pytest.mark.asyncio
    async def test_non_trigger_buffered_not_forwarded(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION))
        result = await gl.push(_msg("just chatting"))
        assert result is True
        inner.enqueue.assert_not_called()
        assert len(gl._buffer) == 1

    @pytest.mark.asyncio
    async def test_always_mode_forwards_everything(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.ALWAYS))
        await gl.push(_msg("just chatting"))
        inner.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_always_mode_no_stripping(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.ALWAYS))
        await gl.push(_msg("hello world"))
        forwarded = inner.enqueue.call_args[0][0]
        assert forwarded.text == "hello world"

    @pytest.mark.asyncio
    async def test_trigger_flushes_buffer(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION, prefix="!"))
        nid = _node_id()
        await gl.push(_msg("context one", node_id=nid, username="alice"))
        await gl.push(_msg("context two", node_id=nid, username="bob"))
        assert len(gl._buffer) == 2
        inner.enqueue.assert_not_called()

        await gl.push(_msg("!what did they say?", node_id=nid, username="carol"))
        inner.enqueue.assert_called_once()
        forwarded = inner.enqueue.call_args[0][0]
        assert "[alice]: context one" in forwarded.text
        assert "[bob]: context two" in forwarded.text
        assert "what did they say?" in forwarded.text
        assert len(gl._buffer) == 0

    @pytest.mark.asyncio
    async def test_trigger_with_empty_buffer(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION, prefix="!"))
        await gl.push(_msg("!solo trigger"))
        forwarded = inner.enqueue.call_args[0][0]
        assert forwarded.text == "solo trigger"

    @pytest.mark.asyncio
    async def test_mention_stripped_from_forwarded_text(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION))
        await gl.push(_msg("@bot:matrix.org what time is it"))
        forwarded = inner.enqueue.call_args[0][0]
        assert "@bot" not in forwarded.text
        assert "what time is it" in forwarded.text

    @pytest.mark.asyncio
    async def test_localpart_mention_stripped(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION))
        await gl.push(_msg("@bot do the thing"))
        forwarded = inner.enqueue.call_args[0][0]
        assert "@bot" not in forwarded.text
        assert "do the thing" in forwarded.text

    @pytest.mark.asyncio
    async def test_backpressure_propagated(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION, prefix="!"))
        inner.enqueue = AsyncMock(return_value=False)
        result = await gl.push(_msg("!hello"))
        assert result is False

    @pytest.mark.asyncio
    async def test_multiple_triggers_each_get_own_buffer(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION, prefix="!"))
        nid = _node_id()
        await gl.push(_msg("ctx1", node_id=nid))
        await gl.push(_msg("!trigger one", node_id=nid))
        await gl.push(_msg("ctx2", node_id=nid))
        await gl.push(_msg("!trigger two", node_id=nid))

        assert inner.enqueue.call_count == 2
        first_text  = inner.enqueue.call_args_list[0][0][0].text
        second_text = inner.enqueue.call_args_list[1][0][0].text
        assert "ctx1" in first_text and "trigger one" in first_text
        assert "ctx2" in second_text and "trigger two" in second_text

    @pytest.mark.asyncio
    async def test_prefix_mode_non_prefix_not_forwarded(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.PREFIX, prefix="!"))
        await gl.push(_msg("@bot:matrix.org hello"))
        inner.enqueue.assert_not_called()
        assert len(gl._buffer) == 1


# ---------------------------------------------------------------------------
# GroupLane — reset
# ---------------------------------------------------------------------------

class TestGroupLaneReset:
    def test_reset_clears_buffer(self):
        gl, inner = _make_lane()
        gl._buffer.append(_msg("some context"))
        gl.reset()
        assert gl._buffer == []

    def test_reset_calls_inner_reset(self):
        gl, inner = _make_lane()
        gl.reset()
        inner.reset.assert_called_once()

    def test_reset_cancels_timeout(self):
        gl, inner = _make_lane(_policy(buffer_timeout_s=60.0))
        fake_task = MagicMock()
        fake_task.done.return_value = False
        gl._timeout_task = fake_task
        gl.reset()
        fake_task.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# GroupLane — set_activation
# ---------------------------------------------------------------------------

class TestGroupLaneActivationToggle:
    def test_set_activation_changes_mode(self):
        gl, _ = _make_lane(_policy(activation=ActivationMode.MENTION))
        gl.set_activation(ActivationMode.ALWAYS)
        assert gl._policy.activation == ActivationMode.ALWAYS

    def test_set_activation_preserves_other_fields(self):
        p = _policy(prefix=">>", bot_mxid="@mybot:example.com", buffer_timeout_s=5.0)
        gl, _ = _make_lane(p)
        gl.set_activation(ActivationMode.PREFIX)
        assert gl._policy.trigger_prefix == ">>"
        assert gl._policy.bot_mxid == "@mybot:example.com"
        assert gl._policy.buffer_timeout_s == 5.0

    @pytest.mark.asyncio
    async def test_always_after_toggle_forwards_untriggered(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION))
        gl.set_activation(ActivationMode.ALWAYS)
        await gl.push(_msg("no trigger here"))
        inner.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_mention_after_toggle_filters_again(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.ALWAYS))
        gl.set_activation(ActivationMode.MENTION)
        await gl.push(_msg("no trigger here"))
        inner.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# GroupLane — timeout flush
# ---------------------------------------------------------------------------

class TestGroupLaneTimeoutFlush:
    @pytest.mark.asyncio
    async def test_timeout_flushes_buffer(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION, buffer_timeout_s=0.05))
        await gl.push(_msg("buffered line"))
        assert len(gl._buffer) == 1
        inner.enqueue.assert_not_called()

        await asyncio.sleep(0.15)
        inner.enqueue.assert_called_once()
        forwarded = inner.enqueue.call_args[0][0]
        assert "buffered line" in forwarded.text

    @pytest.mark.asyncio
    async def test_trigger_before_timeout_cancels_it(self):
        gl, inner = _make_lane(_policy(activation=ActivationMode.MENTION, prefix="!", buffer_timeout_s=0.5))
        await gl.push(_msg("buffered line"))
        await gl.push(_msg("!trigger"))

        assert inner.enqueue.call_count == 1
        await asyncio.sleep(0.6)
        assert inner.enqueue.call_count == 1


# ---------------------------------------------------------------------------
# Router integration — GroupLane created for group sessions
# ---------------------------------------------------------------------------

class TestRouterGroupLaneIntegration:
    @pytest.fixture
    def gw(self):
        cfg = _make_config()

        async def _noop_run(msg, abort_event=None):
            return
            yield

        with patch("router.AgentLoop") as MockLoop:
            instance = MagicMock()
            instance.run = _noop_run
            instance.gateway = None
            instance.reset = MagicMock()
            instance._tail_node_id = "stub-node"
            MockLoop.return_value = instance
            router = Router(config=cfg)
            router._mock_loop_instance = instance
            yield router

    @pytest.mark.asyncio
    async def test_group_message_with_policy_creates_group_lane(self, gw):
        nid = _node_id()
        p   = _policy(activation=ActivationMode.MENTION, prefix="!")
        msg = _msg("!hello", node_id=nid, policy=p)
        gw.register_platform_handler("matrix", AsyncMock())
        await gw.push(msg)
        await asyncio.sleep(0)
        lane = gw._lane_router._lanes.get(nid)
        assert isinstance(lane, GroupLane)

    @pytest.mark.asyncio
    async def test_dm_message_creates_plain_lane(self, gw):
        nid = _node_id()
        gw.register_platform_handler("matrix", AsyncMock())
        gw._node_platforms[nid] = "matrix"
        msg = InboundMessage(
            tail_node_id=nid,
            author=_make_user(),
            content_type=ContentType.TEXT,
            text="hello",
            message_id="m1",
            timestamp=time.time(),
        )
        await gw.push(msg)
        await asyncio.sleep(0)
        lane = gw._lane_router._lanes.get(nid)
        assert isinstance(lane, Lane)

    @pytest.mark.asyncio
    async def test_group_without_policy_creates_plain_lane(self, gw):
        nid = _node_id()
        gw.register_platform_handler("matrix", AsyncMock())
        msg = InboundMessage(
            tail_node_id=nid,
            author=_make_user(),
            content_type=ContentType.TEXT,
            text="hello",
            message_id="m1",
            timestamp=time.time(),
            group_policy=None,
        )
        await gw.push(msg)
        await asyncio.sleep(0)
        lane = gw._lane_router._lanes.get(nid)
        assert isinstance(lane, Lane)

    @pytest.mark.asyncio
    async def test_set_group_activation_updates_lane(self, gw):
        nid = _node_id()
        p   = _policy(activation=ActivationMode.MENTION, prefix="!")
        msg = _msg("!hello", node_id=nid, policy=p)
        gw.register_platform_handler("matrix", AsyncMock())
        await gw.push(msg)
        await asyncio.sleep(0)

        result = gw.set_group_activation(nid, "always")
        assert result is True
        lane = gw._lane_router._lanes[nid]
        assert lane._policy.activation == ActivationMode.ALWAYS

    def test_set_group_activation_returns_false_for_unknown_node(self, gw):
        result = gw.set_group_activation(_node_id(), "always")
        assert result is False

    def test_set_group_activation_invalid_mode_returns_false(self, gw):
        result = gw.set_group_activation(_node_id(), "invalid_mode")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_group_activation_on_plain_lane_returns_false(self, gw):
        nid = _node_id()
        gw.register_platform_handler("matrix", AsyncMock())
        gw._node_platforms[nid] = "matrix"
        msg = InboundMessage(
            tail_node_id=nid,
            author=_make_user(),
            content_type=ContentType.TEXT,
            text="hello",
            message_id="m1",
            timestamp=time.time(),
        )
        await gw.push(msg)
        await asyncio.sleep(0)
        result = gw.set_group_activation(nid, "always")
        assert result is False  # plain Lane, not GroupLane

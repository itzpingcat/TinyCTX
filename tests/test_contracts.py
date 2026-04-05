"""
tests/test_contracts.py

Tests for pure data types in contracts.py.
SessionKey and ChatType have been removed (Phase 2 tree refactor).
InboundMessage now uses tail_node_id (str) as its routing key.

Run with:
    pytest tests/
"""
import pytest
from TinyCTX.contracts import (
    UserIdentity, InboundMessage,
    AgentTextFinal, AgentError,
    ToolCall, ToolResult,
    Platform, ContentType,
    content_type_for,
)
import time
import uuid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_id():
    return str(uuid.uuid4())


def _make_msg(node_id=None, text="hello"):
    return InboundMessage(
        tail_node_id=node_id or _node_id(),
        author=UserIdentity(Platform.CLI, "u1", "alice"),
        content_type=ContentType.TEXT,
        text=text,
        message_id="msg-1",
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# InboundMessage
# ---------------------------------------------------------------------------

class TestInboundMessage:
    def test_tail_node_id_stored(self):
        nid = _node_id()
        msg = _make_msg(node_id=nid)
        assert msg.tail_node_id == nid

    def test_frozen_immutability(self):
        msg = _make_msg()
        with pytest.raises((AttributeError, TypeError)):
            msg.text = "changed"  # type: ignore

    def test_trace_id_auto_generated(self):
        a = _make_msg()
        b = _make_msg()
        assert a.trace_id != b.trace_id

    def test_attachments_default_empty_tuple(self):
        msg = _make_msg()
        assert msg.attachments == ()

    def test_content_type_text(self):
        assert content_type_for("hello", False) == ContentType.TEXT

    def test_content_type_mixed(self):
        assert content_type_for("hello", True) == ContentType.MIXED

    def test_content_type_attachment_only(self):
        assert content_type_for("", True) == ContentType.ATTACHMENT_ONLY


# ---------------------------------------------------------------------------
# ToolCall
# ---------------------------------------------------------------------------

class TestToolCall:
    def test_make_generates_unique_ids(self):
        a = ToolCall.make("fn", {})
        b = ToolCall.make("fn", {})
        assert a.call_id != b.call_id

    def test_make_stores_name_and_args(self):
        tc = ToolCall.make("search", {"q": "cats"})
        assert tc.tool_name == "search"
        assert tc.args == {"q": "cats"}

    def test_frozen(self):
        tc = ToolCall(call_id="x", tool_name="fn", args={})
        with pytest.raises((AttributeError, TypeError)):
            tc.tool_name = "other"  # type: ignore


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

class TestToolResult:
    def test_defaults(self):
        r = ToolResult(call_id="x", tool_name="fn", output="ok")
        assert r.is_error is False

    def test_error_flag(self):
        r = ToolResult(call_id="x", tool_name="fn", output="boom", is_error=True)
        assert r.is_error is True


# ---------------------------------------------------------------------------
# Platform enum
# ---------------------------------------------------------------------------

class TestPlatform:
    def test_platform_values(self):
        assert Platform.CLI.value == "cli"
        assert Platform.DISCORD.value == "discord"
        assert Platform.MATRIX.value == "matrix"
        assert Platform.CRON.value == "cron"
        assert Platform.API.value == "api"

    def test_platform_from_value(self):
        assert Platform("cli") == Platform.CLI
        assert Platform("cron") == Platform.CRON
        assert Platform("api") == Platform.API


# ---------------------------------------------------------------------------
# AgentEvent base fields
# ---------------------------------------------------------------------------

class TestAgentEventBase:
    def test_agent_text_final_fields(self):
        nid = _node_id()
        ev = AgentTextFinal(
            tail_node_id=nid,
            trace_id="t1",
            reply_to_message_id="m1",
            text="hello",
        )
        assert ev.tail_node_id == nid
        assert ev.text == "hello"

    def test_agent_error_fields(self):
        nid = _node_id()
        ev = AgentError(
            tail_node_id=nid,
            trace_id="t1",
            reply_to_message_id="m1",
            message="boom",
        )
        assert ev.message == "boom"

"""
tests/test_contracts.py

Tests for contracts.py — pure dataclasses/enums with no I/O. Covers
construction, defaults, enum values, and the content_type_for() helper.

Run with:
    pytest tests/
"""
from __future__ import annotations

from TinyCTX.contracts import (
    Platform,
    ContentType,
    AttachmentKind,
    content_type_for,
    UserIdentity,
    Attachment,
    SessionEnvironment,
    InboundMessage,
    AgentThinkingChunk,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    AgentError,
    AgentOutboundFiles,
    ToolCall,
    ToolResult,
    IMAGE_BLOCK_PREFIX,
    MANUAL_LAUNCH_ATTR,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TestPlatform:
    def test_values(self):
        assert Platform.CLI == "cli"
        assert Platform.DISCORD == "discord"
        assert Platform.MATRIX == "matrix"
        assert Platform.TELEGRAM == "telegram"
        assert Platform.CRON == "cron"
        assert Platform.API == "api"
        assert Platform.SYSTEM == "system"

    def test_is_str_enum(self):
        assert isinstance(Platform.CLI, str)


class TestContentType:
    def test_values(self):
        assert ContentType.TEXT == "text"
        assert ContentType.MIXED == "mixed"
        assert ContentType.ATTACHMENT_ONLY == "attachment_only"


class TestAttachmentKind:
    def test_values(self):
        assert AttachmentKind.IMAGE == "image"
        assert AttachmentKind.TEXT == "text"
        assert AttachmentKind.DOCUMENT == "document"
        assert AttachmentKind.BINARY == "binary"


class TestContentTypeFor:
    def test_text_only(self):
        assert content_type_for("hello", False) == ContentType.TEXT

    def test_attachment_only(self):
        assert content_type_for("", True) == ContentType.ATTACHMENT_ONLY

    def test_mixed(self):
        assert content_type_for("hello", True) == ContentType.MIXED

    def test_no_text_no_attachments(self):
        assert content_type_for("", False) == ContentType.TEXT


# ---------------------------------------------------------------------------
# User identity / attachments / session environment
# ---------------------------------------------------------------------------

class TestUserIdentity:
    def test_construction(self):
        u = UserIdentity(platform=Platform.CLI, user_id="u1", username="Alice")
        assert u.platform == Platform.CLI
        assert u.user_id == "u1"
        assert u.username == "Alice"

    def test_is_frozen(self):
        u = UserIdentity(platform=Platform.CLI, user_id="u1", username="Alice")
        try:
            u.user_id = "other"
            assert False, "should be frozen"
        except Exception:
            pass


class TestAttachment:
    def test_defaults(self):
        a = Attachment(filename="f.txt", data=b"hi", mime_type="text/plain")
        assert a.kind == AttachmentKind.BINARY

    def test_explicit_kind(self):
        a = Attachment(filename="f.png", data=b"\x89PNG", mime_type="image/png", kind=AttachmentKind.IMAGE)
        assert a.kind == AttachmentKind.IMAGE


class TestSessionEnvironment:
    def test_defaults(self):
        env = SessionEnvironment(platform=Platform.CLI)
        assert env.agent_name is None
        assert env.server_name is None
        assert env.channel_name is None


# ---------------------------------------------------------------------------
# InboundMessage
# ---------------------------------------------------------------------------

class TestInboundMessage:
    def _msg(self, **overrides):
        defaults = dict(
            tail_node_id="node1",
            author="alice",
            env=SessionEnvironment(platform=Platform.CLI),
            content_type=ContentType.TEXT,
            text="hi",
            message_id="m1",
            timestamp=123.0,
        )
        defaults.update(overrides)
        return InboundMessage(**defaults)

    def test_defaults(self):
        m = self._msg()
        assert m.trigger is True
        assert m.reply_to_id is None
        assert m.reply_to_author is None
        assert m.attachments == ()
        assert m.trace_id  # auto-generated uuid string

    def test_trace_ids_unique(self):
        a = self._msg()
        b = self._msg()
        assert a.trace_id != b.trace_id

    def test_attachments_passed_through(self):
        att = Attachment(filename="a.txt", data=b"x", mime_type="text/plain")
        m = self._msg(attachments=(att,))
        assert m.attachments == (att,)


# ---------------------------------------------------------------------------
# Agent event stream
# ---------------------------------------------------------------------------

class TestAgentEvents:
    def _base_kwargs(self):
        return dict(tail_node_id="n1", trace_id="t1", reply_to_message_id="m1")

    def test_thinking_chunk(self):
        e = AgentThinkingChunk(**self._base_kwargs(), text="thinking")
        assert e.text == "thinking"
        assert e.tail_node_id == "n1"

    def test_text_chunk(self):
        e = AgentTextChunk(**self._base_kwargs(), text="hi")
        assert e.text == "hi"

    def test_text_final_defaults(self):
        e = AgentTextFinal(**self._base_kwargs(), text="done")
        assert e.suppressed is False

    def test_text_final_suppressed(self):
        e = AgentTextFinal(**self._base_kwargs(), text="", suppressed=True)
        assert e.suppressed is True

    def test_tool_call(self):
        e = AgentToolCall(**self._base_kwargs(), call_id="c1", tool_name="shell", args={"cmd": "ls"})
        assert e.call_id == "c1"
        assert e.tool_name == "shell"
        assert e.args == {"cmd": "ls"}

    def test_tool_result_defaults(self):
        e = AgentToolResult(**self._base_kwargs(), call_id="c1", tool_name="shell", output="ok")
        assert e.is_error is False

    def test_tool_result_error(self):
        e = AgentToolResult(**self._base_kwargs(), call_id="c1", tool_name="shell", output="boom", is_error=True)
        assert e.is_error is True

    def test_error_event(self):
        e = AgentError(**self._base_kwargs(), message="failed")
        assert e.message == "failed"

    def test_outbound_files(self):
        e = AgentOutboundFiles(**self._base_kwargs(), paths=("a.txt", "b.txt"))
        assert e.paths == ("a.txt", "b.txt")


# ---------------------------------------------------------------------------
# Sentinel values
# ---------------------------------------------------------------------------

class TestSentinels:
    def test_image_block_prefix(self):
        assert IMAGE_BLOCK_PREFIX == "IMAGE_BLOCK:"

    def test_manual_launch_attr(self):
        assert MANUAL_LAUNCH_ATTR == "MANUAL_LAUNCH"


# ---------------------------------------------------------------------------
# ToolCall / ToolResult
# ---------------------------------------------------------------------------

class TestToolCall:
    def test_make_generates_call_id(self):
        tc = ToolCall.make("shell", {"cmd": "ls"})
        assert tc.tool_name == "shell"
        assert tc.args == {"cmd": "ls"}
        assert tc.call_id  # non-empty uuid string

    def test_make_ids_unique(self):
        a = ToolCall.make("foo", {})
        b = ToolCall.make("foo", {})
        assert a.call_id != b.call_id


class TestToolResult:
    def test_defaults(self):
        tr = ToolResult(call_id="c1", tool_name="foo", output="result")
        assert tr.is_error is False
        assert tr.is_image is False
        assert tr.image_mime is None
        assert tr.image_b64 is None

    def test_image_result(self):
        tr = ToolResult(
            call_id="c1", tool_name="screenshot", output="",
            is_image=True, image_mime="image/png", image_b64="abc123",
        )
        assert tr.is_image is True
        assert tr.image_mime == "image/png"
        assert tr.image_b64 == "abc123"

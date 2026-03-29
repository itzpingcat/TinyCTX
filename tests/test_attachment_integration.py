"""
tests/test_attachment_integration.py

Tests for the attachment plumbing that spans contracts, config, context,
and agent — everything except utils/attachments.py itself (that lives in
test_attachments.py).

Topics:
  - Attachment / AttachmentKind dataclasses (contracts)
  - InboundMessage.attachments field
  - ModelConfig.vision + AttachmentConfig (config)
  - HistoryEntry with list content (context)
  - Context merge guard for content-block lists (context)
  - _count_tokens handles list content (context)
  - AgentLoop Stage-1 calls build_content_blocks when attachments present (agent)
  - AgentLoop _flush_history round-trips list content (agent)

No LLM calls.  No real filesystem outside tmp_path.

Run with:
    pytest tests/test_attachment_integration.py -v
"""
from __future__ import annotations

import base64
import json
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from contracts import (
    Attachment, AttachmentKind,
    InboundMessage, ContentType,
    SessionKey, UserIdentity, Platform,
)
from config import ModelConfig, AttachmentConfig, Config, LLMRoutingConfig
from context import Context, HistoryEntry, ROLE_USER, ROLE_ASSISTANT
from ai import TextDelta


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _att(filename="file.txt", data=b"hello", mime_type="text/plain") -> Attachment:
    return Attachment(filename=filename, data=data, mime_type=mime_type)


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI6QAAAABJRU5ErkJggg=="
    )


def _make_msg(text="hi", attachments=()) -> InboundMessage:
    from contracts import content_type_for
    return InboundMessage(
        session_key=SessionKey.dm("u1"),
        author=UserIdentity(platform=Platform.CLI, user_id="u1", username="alice"),
        content_type=content_type_for(text, bool(attachments)),
        text=text,
        message_id="msg-1",
        timestamp=0.0,
        attachments=tuple(attachments),
    )


def _make_full_config(tmp_path: Path, vision: bool = False) -> Config:
    """Minimal Config with real AttachmentConfig wired in."""
    mc = ModelConfig(
        model="test",
        base_url="http://localhost/v1",
        api_key_env="N/A",
        vision=vision,
    )
    return Config(
        models={"primary": mc},
        llm=LLMRoutingConfig(primary="primary"),
        attachments=AttachmentConfig(
            inline_max_files=3,
            inline_max_bytes=200 * 1024,
            uploads_dir="uploads",
        ),
        workspace=__import__("config").WorkspaceConfig(path=tmp_path / "workspace"),
    )


# ---------------------------------------------------------------------------
# Attachment contract
# ---------------------------------------------------------------------------

class TestAttachmentContract:
    def test_attachment_is_frozen(self):
        att = _att()
        with pytest.raises((AttributeError, TypeError)):
            att.filename = "changed"  # type: ignore

    def test_attachment_kind_default_is_binary(self):
        att = _att()
        assert att.kind == AttachmentKind.BINARY

    def test_attachment_kind_can_be_set(self):
        att = Attachment(filename="img.png", data=b"", mime_type="image/png", kind=AttachmentKind.IMAGE)
        assert att.kind == AttachmentKind.IMAGE

    def test_attachment_kind_values(self):
        assert AttachmentKind.IMAGE    == "image"
        assert AttachmentKind.TEXT     == "text"
        assert AttachmentKind.DOCUMENT == "document"
        assert AttachmentKind.BINARY   == "binary"


# ---------------------------------------------------------------------------
# InboundMessage with attachments
# ---------------------------------------------------------------------------

class TestInboundMessageAttachments:
    def test_attachments_default_empty_tuple(self):
        msg = _make_msg()
        assert msg.attachments == ()

    def test_attachments_stored(self):
        atts = (_att("a.txt"), _att("b.txt"))
        msg = _make_msg(attachments=atts)
        assert len(msg.attachments) == 2
        assert msg.attachments[0].filename == "a.txt"

    def test_inbound_message_is_frozen(self):
        msg = _make_msg()
        with pytest.raises((AttributeError, TypeError)):
            msg.text = "changed"  # type: ignore

    def test_content_type_mixed_when_text_and_attachments(self):
        msg = _make_msg(text="hi", attachments=(_att(),))
        assert msg.content_type == ContentType.MIXED

    def test_content_type_attachment_only_when_no_text(self):
        msg = _make_msg(text="", attachments=(_att(),))
        assert msg.content_type == ContentType.ATTACHMENT_ONLY

    def test_content_type_text_when_no_attachments(self):
        msg = _make_msg(text="hello")
        assert msg.content_type == ContentType.TEXT


# ---------------------------------------------------------------------------
# ModelConfig.vision
# ---------------------------------------------------------------------------

class TestModelConfigVision:
    def test_vision_defaults_false(self):
        m = ModelConfig(model="x", base_url="http://x")
        assert m.vision is False

    def test_vision_can_be_set_true(self):
        m = ModelConfig(model="x", base_url="http://x", vision=True)
        assert m.vision is True

    def test_supports_vision_property(self):
        m = ModelConfig(model="x", base_url="http://x", vision=True)
        assert m.supports_vision is True

    def test_vision_parsed_from_yaml(self, tmp_path):
        from config import load
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""\
            models:
              smart:
                base_url: https://api.anthropic.com/v1
                model: claude-sonnet-4-20250514
                api_key_env: N/A
                vision: true
            llm:
              primary: smart
        """))
        cfg = load(str(p))
        assert cfg.models["smart"].vision is True

    def test_vision_false_when_omitted_from_yaml(self, tmp_path):
        from config import load
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""\
            models:
              local:
                base_url: http://localhost/v1
                model: llama3
                api_key_env: N/A
            llm:
              primary: local
        """))
        cfg = load(str(p))
        assert cfg.models["local"].vision is False


# ---------------------------------------------------------------------------
# AttachmentConfig
# ---------------------------------------------------------------------------

class TestAttachmentConfig:
    def test_defaults(self):
        cfg = AttachmentConfig()
        assert cfg.inline_max_files == 3
        assert cfg.inline_max_bytes == 200 * 1024
        assert cfg.uploads_dir == "uploads"

    def test_custom_values(self):
        cfg = AttachmentConfig(inline_max_files=1, inline_max_bytes=512, uploads_dir="files")
        assert cfg.inline_max_files == 1
        assert cfg.inline_max_bytes == 512
        assert cfg.uploads_dir == "files"

    def test_parsed_from_yaml(self, tmp_path):
        from config import load
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""\
            models:
              main:
                base_url: http://localhost/v1
                model: llama3
                api_key_env: N/A
            llm:
              primary: main
            attachments:
              inline_max_files: 5
              inline_max_bytes: 1048576
              uploads_dir: my_uploads
        """))
        cfg = load(str(p))
        assert cfg.attachments.inline_max_files == 5
        assert cfg.attachments.inline_max_bytes == 1_048_576
        assert cfg.attachments.uploads_dir == "my_uploads"

    def test_defaults_applied_when_section_absent(self, tmp_path):
        from config import load
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""\
            models:
              main:
                base_url: http://localhost/v1
                model: llama3
                api_key_env: N/A
            llm:
              primary: main
        """))
        cfg = load(str(p))
        assert cfg.attachments.inline_max_files == 3


# ---------------------------------------------------------------------------
# HistoryEntry with list content (context changes)
# ---------------------------------------------------------------------------

class TestHistoryEntryListContent:
    def test_user_entry_accepts_list(self):
        blocks = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:..."}}]
        e = HistoryEntry.user(blocks)
        assert e.content == blocks

    def test_user_entry_accepts_string(self):
        e = HistoryEntry.user("plain text")
        assert e.content == "plain text"

    def test_render_list_content_passed_through(self):
        blocks = [{"type": "text", "text": "hello"}]
        e = HistoryEntry.user(blocks)
        ctx = Context()
        ctx.add(e)
        messages = ctx.assemble()
        assert messages[0]["content"] == blocks

    def test_render_string_content_passed_through(self):
        e = HistoryEntry.user("plain")
        ctx = Context()
        ctx.add(e)
        messages = ctx.assemble()
        assert messages[0]["content"] == "plain"


# ---------------------------------------------------------------------------
# Context: no merging of content-block list entries
# ---------------------------------------------------------------------------

class TestContextNoMergeBlocks:
    def test_two_plain_string_users_merge(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("first"))
        ctx.add(HistoryEntry.user("second"))
        messages = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1

    def test_list_content_user_not_merged_with_string(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("plain"))
        ctx.add(HistoryEntry.user([{"type": "text", "text": "block"}]))
        messages = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 2

    def test_two_list_content_users_not_merged(self):
        ctx = Context()
        blocks = [{"type": "text", "text": "block"}]
        ctx.add(HistoryEntry.user(blocks))
        ctx.add(HistoryEntry.user(blocks))
        messages = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 2

    def test_string_content_users_between_list_content_not_merged(self):
        ctx = Context()
        ctx.add(HistoryEntry.user([{"type": "text", "text": "block"}]))
        ctx.add(HistoryEntry.user("plain after block"))
        messages = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 2


# ---------------------------------------------------------------------------
# Context: _count_tokens handles list content
# ---------------------------------------------------------------------------

class TestCountTokensListContent:
    def test_list_content_counted(self):
        ctx = Context()
        blocks = [
            {"type": "text", "text": "A" * 400},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 1000}},
        ]
        ctx.add(HistoryEntry.user(blocks))
        messages = ctx.assemble()
        assert ctx.state["tokens_used"] > 0

    def test_list_content_counts_more_than_short_string(self):
        ctx_list = Context()
        blocks = [{"type": "text", "text": "A" * 400}, {"type": "image_url", "image_url": {"url": "data:..." + "B" * 400}}]
        ctx_list.add(HistoryEntry.user(blocks))
        ctx_list.assemble()

        ctx_str = Context()
        ctx_str.add(HistoryEntry.user("short"))
        ctx_str.assemble()

        assert ctx_list.state["tokens_used"] > ctx_str.state["tokens_used"]

    def test_token_budget_trims_list_content_turns(self):
        """A user turn with a large content-block list should be dropped when over budget."""
        ctx = Context(token_limit=20)
        big_blocks = [{"type": "text", "text": "X" * 500}]
        ctx.add(HistoryEntry.user(big_blocks))
        ctx.add(HistoryEntry.user("short recent message"))
        messages = ctx.assemble()
        # The recent short message must survive
        assert any(
            (isinstance(m["content"], str) and "short recent message" in m["content"])
            or (isinstance(m["content"], list) and any("short recent message" in b.get("text", "") for b in m["content"]))
            for m in messages
        )


# ---------------------------------------------------------------------------
# AgentLoop: Stage-1 intake with attachments
# ---------------------------------------------------------------------------

class TestAgentIntakeWithAttachments:
    def _make_agent(self, tmp_path, vision=False):
        from agent import AgentLoop

        cfg = _make_full_config(tmp_path, vision=vision)
        cfg.workspace.path.mkdir(parents=True, exist_ok=True)

        counter = {"n": 0}

        async def _text_stream(messages, tools=None):
            yield TextDelta(text="ok")

        llm_mock = MagicMock()
        llm_mock.stream = _text_stream

        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=llm_mock):
                agent = AgentLoop(
                    session_key=SessionKey.dm(f"att-test"),
                    config=cfg,
                )
        agent._models["primary"] = llm_mock

        return agent

    @pytest.mark.asyncio
    async def test_plain_message_adds_string_to_context(self, tmp_path):
        agent = self._make_agent(tmp_path)
        msg = _make_msg("hello")
        async for _ in agent.run(msg):
            pass
        user_entries = [e for e in agent.context.dialogue if e.role == "user"]
        assert len(user_entries) == 1
        assert user_entries[0].content == "hello"

    @pytest.mark.asyncio
    async def test_text_attachment_adds_list_to_context(self, tmp_path):
        agent = self._make_agent(tmp_path)
        att = _att("notes.txt", data=b"note content", mime_type="text/plain")
        msg = _make_msg("here are my notes", attachments=(att,))
        async for _ in agent.run(msg):
            pass
        user_entries = [e for e in agent.context.dialogue if e.role == "user"]
        assert len(user_entries) == 1
        # Content should be a list (content blocks) since there is an attachment
        assert isinstance(user_entries[0].content, list)

    @pytest.mark.asyncio
    async def test_text_attachment_content_present_in_blocks(self, tmp_path):
        agent = self._make_agent(tmp_path)
        att = _att("file.txt", data=b"the file body", mime_type="text/plain")
        msg = _make_msg("check file", attachments=(att,))
        async for _ in agent.run(msg):
            pass
        user_entry = next(e for e in agent.context.dialogue if e.role == "user")
        full = json.dumps(user_entry.content)
        assert "the file body" in full

    @pytest.mark.asyncio
    async def test_image_attachment_vision_model_inlines_image(self, tmp_path):
        agent = self._make_agent(tmp_path, vision=True)
        att = _att("photo.png", data=_png_bytes(), mime_type="image/png")
        msg = _make_msg("what is this?", attachments=(att,))
        async for _ in agent.run(msg):
            pass
        user_entry = next(e for e in agent.context.dialogue if e.role == "user")
        assert isinstance(user_entry.content, list)
        img_blocks = [b for b in user_entry.content if b.get("type") == "image_url"]
        assert len(img_blocks) == 1

    @pytest.mark.asyncio
    async def test_image_attachment_non_vision_model_no_image_block(self, tmp_path):
        agent = self._make_agent(tmp_path, vision=False)
        att = _att("photo.png", data=_png_bytes(), mime_type="image/png")
        msg = _make_msg("what is this?", attachments=(att,))
        async for _ in agent.run(msg):
            pass
        user_entry = next(e for e in agent.context.dialogue if e.role == "user")
        if isinstance(user_entry.content, list):
            img_blocks = [b for b in user_entry.content if b.get("type") == "image_url"]
            assert len(img_blocks) == 0

    @pytest.mark.asyncio
    async def test_attachment_saved_to_workspace_uploads(self, tmp_path):
        agent = self._make_agent(tmp_path)
        att = _att("saved.txt", data=b"data")
        msg = _make_msg("attached", attachments=(att,))
        async for _ in agent.run(msg):
            pass
        uploads = agent.config.workspace.path / "uploads"
        assert (uploads / "saved.txt").exists()


# ---------------------------------------------------------------------------
# AgentLoop: _flush_history round-trips list content
# ---------------------------------------------------------------------------

class TestAgentFlushRestoreListContent:
    @pytest.mark.asyncio
    async def test_list_content_survives_db_roundtrip(self, tmp_path):
        """
        List content written to the DB must round-trip back as a list,
        not as a raw JSON string. (Replaces the old _flush_history test.)
        """
        from agent import AgentLoop

        cfg = _make_full_config(tmp_path)
        cfg.workspace.path.mkdir(parents=True, exist_ok=True)

        async def _text_stream(messages, tools=None):
            yield TextDelta(text="ok")

        llm_mock = MagicMock()
        llm_mock.stream = _text_stream

        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=llm_mock):
                agent = AgentLoop(
                    session_key=SessionKey.dm("persist-test"),
                    config=cfg,
                )
        agent._models["primary"] = llm_mock

        # Write a list-content user entry directly via context (goes to DB)
        blocks = [{"type": "text", "text": "user msg"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
        agent.context.add(HistoryEntry.user(blocks))

        # Re-load from DB via assemble() and check the round-trip
        messages = agent.context.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert isinstance(user_msgs[0]["content"], list)
        assert user_msgs[0]["content"][0]["type"] == "text"

    @pytest.mark.asyncio
    async def test_restore_history_preserves_list_content(self, tmp_path):
        """
        History loaded from the DB at agent startup must restore list content
        as a list, not coerce it to a string. (Replaces the old _restore_history test.)
        """
        from agent import AgentLoop
        from db import ConversationDB
        import json as _json

        cfg = _make_full_config(tmp_path)
        cfg.workspace.path.mkdir(parents=True, exist_ok=True)

        blocks = [{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
        sk = SessionKey.dm("restore-test")

        async def _noop_stream(messages, tools=None):
            yield TextDelta(text="ok")

        llm_mock = MagicMock()
        llm_mock.stream = _noop_stream

        # First agent: write a list-content entry to the DB
        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=llm_mock):
                agent1 = AgentLoop(session_key=sk, config=cfg)
        agent1._models["primary"] = llm_mock
        agent1.context.add(HistoryEntry.user(blocks))
        tail_after_write = agent1._tail_node_id

        # Second agent: same session key + same DB — should restore from cursor
        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=llm_mock):
                agent2 = AgentLoop(session_key=sk, config=cfg)

        messages = agent2.context.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert isinstance(user_msgs[0]["content"], list)
        assert user_msgs[0]["content"][0]["type"] == "text"

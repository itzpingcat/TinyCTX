"""
tests/test_agent.py

Tests for AgentLoop — the 6-stage execution loop.

The LLM is fully stubbed via async generator functions so no network calls
are made. Modules are not loaded (MODULES_DIR is patched to a non-existent
path). Each make_agent() call gets a completely fresh AgentLoop with its own
unique session key so tests never bleed into each other.

Phase 1 tree-refactor notes:
  - _flush_history, _restore_history, _load_latest_version, next_session
    are gone. Session-file persistence tests have been removed.
  - reset() only clears in-memory state; it no longer writes to disk.
  - Tests that validated those behaviours now live in test_tree_refactor.py.

Run with:
    python -m pytest tests/
"""
import asyncio
import time
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from contracts import (
    UserIdentity, InboundMessage,
    AgentTextChunk, AgentTextFinal, AgentError, AgentToolResult,
    Platform, ContentType, ToolCall, ToolResult,
)
from context import HistoryEntry
from ai import TextDelta, ToolCallAssembled, LLMError
from db import ConversationDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path):
    cfg = MagicMock()
    primary_mc = MagicMock()
    primary_mc.is_embedding = False
    fast_mc = MagicMock()
    fast_mc.is_embedding = False
    cfg.models = {"primary": primary_mc, "fast": fast_mc}
    cfg.llm.primary = "primary"
    cfg.llm.fallback = []
    cfg.llm.fallback_on.any_error = False
    cfg.llm.fallback_on.http_codes = [429, 500]
    cfg.context = 4096
    cfg.max_tool_cycles = 5
    cfg.workspace.path = str(tmp_path)
    cfg.attachments = MagicMock()
    cfg.get_model_config = MagicMock(return_value=MagicMock(vision=False))
    return cfg


def _make_cursor(tmp_path) -> str:
    """Create a fresh DB cursor node in tmp_path and return its node_id."""
    db   = ConversationDB(tmp_path / "agent.db")
    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content="session:test")
    return node.id


def _make_msg(text="hello", node_id=None, tmp_path=None):
    nid = node_id or "test-node-id"
    return InboundMessage(
        tail_node_id=nid,
        author=UserIdentity(platform=Platform.CLI, user_id="u1", username="alice"),
        content_type=ContentType.TEXT,
        text=text,
        message_id="msg-1",
        timestamp=0.0,
    )


async def _collect(agent, msg):
    """Run the agent and collect all reply events."""
    chunks = []
    async for chunk in agent.run(msg):
        chunks.append(chunk)
    return chunks


def _full_text(chunks):
    return "".join(c.text if hasattr(c, "text") else c.message for c in chunks)


def _text_stream(*texts):
    async def _gen(messages, tools=None):
        for t in texts:
            yield TextDelta(text=t)
    return _gen


def _error_stream(message):
    async def _gen(messages, tools=None):
        yield LLMError(message=message)
    return _gen


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def make_agent(tmp_path):
    """
    Factory fixture — call make_agent(stream_fn) to get a fresh AgentLoop.

    Each call creates a unique cursor node in the shared agent.db so DB state
    never bleeds between tests. The workspace is tmp_path.
    """
    def _factory(stream_fn=None, fallback_stream_fn=None, fallback_names=None):
        from agent import AgentLoop

        node_id = _make_cursor(tmp_path)
        cfg = _make_config(tmp_path)

        if fallback_names:
            cfg.llm.fallback = fallback_names
            cfg.llm.fallback_on.any_error = True

        primary_llm = MagicMock()
        primary_llm.stream = stream_fn or _text_stream("default reply")

        fallback_llm = MagicMock()
        fallback_llm.stream = fallback_stream_fn or _text_stream("fallback reply")

        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=primary_llm):
                agent = AgentLoop(tail_node_id=node_id, config=cfg)

        agent._models["primary"] = primary_llm
        if fallback_names:
            for name in fallback_names:
                agent._models[name] = fallback_llm

        return agent

    return _factory


# ---------------------------------------------------------------------------
# Basic text reply
# ---------------------------------------------------------------------------

class TestBasicReply:
    @pytest.mark.asyncio
    async def test_simple_text_reply(self, make_agent):
        agent = make_agent(_text_stream("hello back"))
        chunks = await _collect(agent, _make_msg("hello", node_id=agent.tail_node_id))
        assert _full_text(chunks) == "hello back"

    @pytest.mark.asyncio
    async def test_reply_is_agent_event(self, make_agent):
        agent = make_agent(_text_stream("hi"))
        chunks = await _collect(agent, _make_msg(node_id=agent.tail_node_id))
        assert all(isinstance(c, (AgentTextChunk, AgentTextFinal, AgentError)) for c in chunks)

    @pytest.mark.asyncio
    async def test_reply_tail_node_id_matches(self, make_agent):
        agent = make_agent(_text_stream("hi"))
        node_id = agent.tail_node_id
        chunks = await _collect(agent, _make_msg(node_id=node_id))
        assert chunks[-1].tail_node_id is not None

    @pytest.mark.asyncio
    async def test_multi_chunk_text_assembled(self, make_agent):
        agent = make_agent(_text_stream("part1", " ", "part2"))
        chunks = await _collect(agent, _make_msg(node_id=agent.tail_node_id))
        assert _full_text(chunks) == "part1 part2"

    @pytest.mark.asyncio
    async def test_turn_count_increments(self, make_agent):
        agent = make_agent(_text_stream("ok"))
        assert agent._turn_count == 0
        await _collect(agent, _make_msg("first", node_id=agent.tail_node_id))
        assert agent._turn_count == 1
        await _collect(agent, _make_msg("second", node_id=agent.tail_node_id))
        assert agent._turn_count == 2


# ---------------------------------------------------------------------------
# Context accumulation
# ---------------------------------------------------------------------------

class TestContextAccumulation:
    @pytest.mark.asyncio
    async def test_user_message_added_to_context(self, make_agent):
        agent = make_agent(_text_stream("ok"))
        await _collect(agent, _make_msg("test input", node_id=agent.tail_node_id))
        roles = [e.role for e in agent.context.dialogue]
        assert "user" in roles

    @pytest.mark.asyncio
    async def test_assistant_reply_added_to_context(self, make_agent):
        agent = make_agent(_text_stream("my reply"))
        await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        assistant_entries = [e for e in agent.context.dialogue if e.role == "assistant"]
        assert len(assistant_entries) == 1
        assert assistant_entries[0].content == "my reply"

    @pytest.mark.asyncio
    async def test_context_grows_across_turns(self, make_agent):
        agent = make_agent(_text_stream("ok"))
        await _collect(agent, _make_msg("turn one", node_id=agent.tail_node_id))
        await _collect(agent, _make_msg("turn two", node_id=agent.tail_node_id))
        assert len(agent.context.dialogue) == 4  # 2 user + 2 assistant


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

class TestToolExecution:
    @pytest.mark.asyncio
    async def test_tool_call_dispatched(self, make_agent):
        call_count = {"n": 0}

        async def stream(messages, tools=None):
            if call_count["n"] == 0:
                call_count["n"] += 1
                yield ToolCallAssembled(call_id="c1", tool_name="my_tool", args={"x": 1})
            else:
                yield TextDelta(text="done")

        agent = make_agent(stream)

        tool_called_with = []

        async def my_tool(x: int) -> str:
            """A test tool. Args: x: input."""
            tool_called_with.append(x)
            return "tool result"

        agent.tool_handler.register_tool(my_tool)
        await _collect(agent, _make_msg("use the tool", node_id=agent.tail_node_id))
        assert tool_called_with == [1]

    @pytest.mark.asyncio
    async def test_tool_result_added_to_context(self, make_agent):
        call_count = {"n": 0}

        async def stream(messages, tools=None):
            if call_count["n"] == 0:
                call_count["n"] += 1
                yield ToolCallAssembled(call_id="c1", tool_name="my_tool", args={})
            else:
                yield TextDelta(text="done")

        agent = make_agent(stream)

        def my_tool() -> str:
            """Tool."""
            return "the answer"

        agent.tool_handler.register_tool(my_tool)
        await _collect(agent, _make_msg("go", node_id=agent.tail_node_id))

        tool_results = [e for e in agent.context.dialogue if e.role == "tool"]
        assert len(tool_results) == 1
        assert "the answer" in tool_results[0].content

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_result(self, make_agent):
        call_count = {"n": 0}

        async def stream(messages, tools=None):
            if call_count["n"] == 0:
                call_count["n"] += 1
                yield ToolCallAssembled(call_id="c1", tool_name="nonexistent", args={})
            else:
                yield TextDelta(text="done")

        agent = make_agent(stream)
        await _collect(agent, _make_msg("go", node_id=agent.tail_node_id))

        tool_results = [e for e in agent.context.dialogue if e.role == "tool"]
        assert len(tool_results) == 1
        assert tool_results[0].content  # some error message

    @pytest.mark.asyncio
    async def test_max_tool_cycles_respected(self, make_agent):
        always_tool_count = {"n": 0}

        async def always_tool(messages, tools=None):
            always_tool_count["n"] += 1
            yield ToolCallAssembled(
                call_id=f"c{always_tool_count['n']}",
                tool_name="loop_tool",
                args={},
            )

        agent = make_agent(always_tool)
        agent.config.max_tool_cycles = 3

        def loop_tool() -> str:
            """Loops forever."""
            return "keep going"

        agent.tool_handler.register_tool(loop_tool)
        chunks = await _collect(agent, _make_msg("go", node_id=agent.tail_node_id))
        text_chunks = [c for c in chunks if isinstance(c, (AgentTextChunk, AgentTextFinal, AgentError))]
        assert any("cycle limit" in c.text.lower() or "tool" in c.text.lower() for c in text_chunks)
        assert always_tool_count["n"] <= 3

    @pytest.mark.asyncio
    async def test_shell_style_failure_output_marks_tool_result_error(self, make_agent):
        call_count = {"n": 0}

        async def stream(messages, tools=None):
            if call_count["n"] == 0:
                call_count["n"] += 1
                yield ToolCallAssembled(call_id="c1", tool_name="my_tool", args={})
            else:
                yield TextDelta(text="done")

        agent = make_agent(stream)

        def my_tool() -> str:
            """Tool."""
            return "[stderr]\nmissing path\n[exit 1]"

        agent.tool_handler.register_tool(my_tool)
        chunks = await _collect(agent, _make_msg("go", node_id=agent.tail_node_id))

        tool_events = [chunk for chunk in chunks if isinstance(chunk, AgentToolResult)]
        assert len(tool_events) == 1
        assert tool_events[0].is_error is True


# ---------------------------------------------------------------------------
# LLM error handling and fallback
# ---------------------------------------------------------------------------

class TestLLMErrors:
    @pytest.mark.asyncio
    async def test_llm_error_surfaces_in_reply(self, make_agent):
        agent = make_agent(_error_stream("HTTP 500: server error"))
        chunks = await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        assert "LLM error" in _full_text(chunks)

    @pytest.mark.asyncio
    async def test_fallback_used_on_error(self, make_agent):
        agent = make_agent(
            stream_fn=_error_stream("HTTP 429: rate limited"),
            fallback_stream_fn=_text_stream("fallback answer"),
            fallback_names=["fast"],
        )
        agent.config.llm.fallback_on.any_error = False
        agent.config.llm.fallback_on.http_codes = [429]

        chunks = await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        assert _full_text(chunks) == "fallback answer"

    @pytest.mark.asyncio
    async def test_no_fallback_on_non_matching_code(self, make_agent):
        agent = make_agent(
            stream_fn=_error_stream("HTTP 404: not found"),
            fallback_stream_fn=_text_stream("should not see this"),
            fallback_names=["fast"],
        )
        agent.config.llm.fallback_on.any_error = False
        agent.config.llm.fallback_on.http_codes = [429, 500]

        chunks = await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        assert "should not see this" not in _full_text(chunks)
        assert "LLM error" in _full_text(chunks)

    @pytest.mark.asyncio
    async def test_any_error_fallback(self, make_agent):
        agent = make_agent(
            stream_fn=_error_stream("Connection failed: timeout"),
            fallback_stream_fn=_text_stream("fallback worked"),
            fallback_names=["fast"],
        )
        agent.config.llm.fallback_on.any_error = True

        chunks = await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        assert _full_text(chunks) == "fallback worked"


# ---------------------------------------------------------------------------
# Reset (in-memory only after tree refactor)
# ---------------------------------------------------------------------------

class TestReset:
    @pytest.mark.asyncio
    async def test_reset_clears_context(self, make_agent):
        agent = make_agent(_text_stream("ok"))
        await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        assert len(agent.context.dialogue) > 0

        agent.reset()
        assert agent.context.dialogue == []

    @pytest.mark.asyncio
    async def test_reset_zeroes_turn_count(self, make_agent):
        agent = make_agent(_text_stream("ok"))
        await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        assert agent._turn_count == 1

        agent.reset()
        assert agent._turn_count == 0

    @pytest.mark.asyncio
    async def test_reset_does_not_delete_db_nodes(self, make_agent, tmp_path):
        """reset() is in-memory only — the tree in agent.db must survive."""
        agent = make_agent(_text_stream("ok"))
        await _collect(agent, _make_msg("hi", node_id=agent.tail_node_id))
        tail_before = agent._tail_node_id

        agent.reset()

        db = ConversationDB(tmp_path / "agent.db")
        assert db.get_node(tail_before) is not None

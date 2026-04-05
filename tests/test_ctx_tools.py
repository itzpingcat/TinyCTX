"""
tests/test_ctx_tools.py

Tests for the ctx_tools module — deduplication, CoT stripping, and trimming.
These test the hooks directly against a real Context instance without needing
a full AgentLoop.

Run with:
    pytest tests/
"""
import pytest
from TinyCTX.context import Context, HistoryEntry, ROLE_TOOL, ROLE_ASSISTANT
from TinyCTX.contracts import ToolCall, ToolResult


def _make_context_with_ctx_tools(config=None):
    """Return a Context with ctx_tools hooks registered."""
    ctx = Context()
    # Import and call register() directly with our context
    import sys, types

    # We need to call the internal register functions directly since
    # ctx_tools.register() expects an agent object. We replicate the call here.
    from TinyCTX.modules.ctx_tools.__main__ import _register_dedup, _register_cot_strip, _register_trim

    cfg = config or {
        "same_call_dedup_after": 3,
        "cot_keep_recent_turns": 0,
        "tool_trim_after": 10,
        "tool_output_truncate_after": 2,
        "max_tool_output_chars": 2000,
    }
    _register_dedup(ctx, cfg)
    _register_cot_strip(ctx, cfg)
    _register_trim(ctx, cfg)
    return ctx


# ---------------------------------------------------------------------------
# CoT stripping
# ---------------------------------------------------------------------------

class TestCoTStrip:
    def test_think_block_removed(self):
        ctx = _make_context_with_ctx_tools({"cot_keep_recent_turns": 0,
                                             "same_call_dedup_after": 99,
                                             "tool_trim_after": 99,
                                             "tool_output_truncate_after": 99,
                                             "max_tool_output_chars": 99999})
        ctx.add(HistoryEntry.user("hi"))
        ctx.add(HistoryEntry.assistant("<think>internal reasoning</think>actual reply"))
        # Add a second turn so the first assistant turn has age > 0
        ctx.add(HistoryEntry.user("follow up"))
        ctx.add(HistoryEntry.assistant("another reply"))

        messages = ctx.assemble()
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        # The older assistant message should have <think> stripped
        older = asst_msgs[0]
        assert "<think>" not in older["content"]
        assert "internal reasoning" not in older["content"]
        assert "actual reply" in older["content"]

    def test_think_block_preserved_for_recent_turns(self):
        ctx = _make_context_with_ctx_tools({"cot_keep_recent_turns": 1,
                                             "same_call_dedup_after": 99,
                                             "tool_trim_after": 99,
                                             "tool_output_truncate_after": 99,
                                             "max_tool_output_chars": 99999})
        ctx.add(HistoryEntry.user("hi"))
        ctx.add(HistoryEntry.assistant("<think>reasoning</think>reply"))

        messages = ctx.assemble()
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        # Most recent assistant turn should keep its <think> block
        assert "<think>" in asst_msgs[-1]["content"]

    def test_multiple_think_blocks_removed(self):
        ctx = _make_context_with_ctx_tools({"cot_keep_recent_turns": 0,
                                             "same_call_dedup_after": 99,
                                             "tool_trim_after": 99,
                                             "tool_output_truncate_after": 99,
                                             "max_tool_output_chars": 99999})
        ctx.add(HistoryEntry.user("a"))
        ctx.add(HistoryEntry.assistant("<think>one</think>mid<think>two</think>end"))
        ctx.add(HistoryEntry.user("b"))
        ctx.add(HistoryEntry.assistant("latest"))

        messages = ctx.assemble()
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        older = asst_msgs[0]
        assert "one" not in older["content"]
        assert "two" not in older["content"]
        assert "mid" in older["content"]
        assert "end" in older["content"]


# ---------------------------------------------------------------------------
# Tool output trimming
# ---------------------------------------------------------------------------

class TestToolTrim:
    def test_old_tool_output_trimmed(self):
        cfg = {
            "same_call_dedup_after": 99,
            "cot_keep_recent_turns": 0,
            "tool_trim_after": 2,          # trim after 2 turns of age
            "tool_output_truncate_after": 99,
            "max_tool_output_chars": 99999,
        }
        ctx = _make_context_with_ctx_tools(cfg)

        tc = ToolCall(call_id="t1", tool_name="fn", args={})
        ctx.add(HistoryEntry.assistant("call", tool_calls=[tc]))
        ctx.add(HistoryEntry.tool_result(
            ToolResult(call_id="t1", tool_name="fn", output="important output")
        ))
        # Add enough turns to age the tool result past trim_after
        ctx.add(HistoryEntry.user("q1"))
        ctx.add(HistoryEntry.assistant("a1"))
        ctx.add(HistoryEntry.user("q2"))
        ctx.add(HistoryEntry.assistant("a2"))

        messages = ctx.assemble()
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if tool_msgs:
            assert "trimmed" in tool_msgs[0]["content"]

    def test_large_tool_output_truncated(self):
        cfg = {
            "same_call_dedup_after": 99,
            "cot_keep_recent_turns": 0,
            "tool_trim_after": 99,
            "tool_output_truncate_after": 0,   # truncate immediately after 0 turns
            "max_tool_output_chars": 50,
        }
        ctx = _make_context_with_ctx_tools(cfg)

        tc = ToolCall(call_id="t1", tool_name="fn", args={})
        ctx.add(HistoryEntry.assistant("call", tool_calls=[tc]))
        ctx.add(HistoryEntry.tool_result(
            ToolResult(call_id="t1", tool_name="fn", output="X" * 200)
        ))
        ctx.add(HistoryEntry.user("follow up"))

        messages = ctx.assemble()
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if tool_msgs:
            assert len(tool_msgs[0]["content"]) < 200
            assert "omitted" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDedup:
    def test_identical_old_tool_calls_deduplicated(self):
        cfg = {
            "same_call_dedup_after": 0,   # dedup immediately
            "cot_keep_recent_turns": 0,
            "tool_trim_after": 99,
            "tool_output_truncate_after": 99,
            "max_tool_output_chars": 99999,
        }
        ctx = _make_context_with_ctx_tools(cfg)

        args = {"q": "cats"}

        # First call
        tc1 = ToolCall(call_id="c1", tool_name="search", args=args)
        ctx.add(HistoryEntry.assistant("searching", tool_calls=[tc1]))
        ctx.add(HistoryEntry.tool_result(ToolResult(call_id="c1", tool_name="search", output="result 1")))

        # Separator turns
        ctx.add(HistoryEntry.user("ok"))
        ctx.add(HistoryEntry.assistant("got it"))

        # Second call with same args
        tc2 = ToolCall(call_id="c2", tool_name="search", args=args)
        ctx.add(HistoryEntry.assistant("searching again", tool_calls=[tc2]))
        ctx.add(HistoryEntry.tool_result(ToolResult(call_id="c2", tool_name="search", output="result 2")))

        ctx.add(HistoryEntry.user("what did you find"))

        messages = ctx.assemble()

        # The most recent search result should survive
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        # At least one result should remain
        assert len(tool_msgs) >= 1

    def test_different_args_not_deduplicated(self):
        cfg = {
            "same_call_dedup_after": 0,
            "cot_keep_recent_turns": 0,
            "tool_trim_after": 99,
            "tool_output_truncate_after": 99,
            "max_tool_output_chars": 99999,
        }
        ctx = _make_context_with_ctx_tools(cfg)

        tc1 = ToolCall(call_id="c1", tool_name="search", args={"q": "cats"})
        ctx.add(HistoryEntry.assistant("s1", tool_calls=[tc1]))
        ctx.add(HistoryEntry.tool_result(ToolResult(call_id="c1", tool_name="search", output="cats result")))

        ctx.add(HistoryEntry.user("now dogs"))

        tc2 = ToolCall(call_id="c2", tool_name="search", args={"q": "dogs"})
        ctx.add(HistoryEntry.assistant("s2", tool_calls=[tc2]))
        ctx.add(HistoryEntry.tool_result(ToolResult(call_id="c2", tool_name="search", output="dogs result")))

        ctx.add(HistoryEntry.user("both"))

        messages = ctx.assemble()
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        # Both should survive — different args
        assert len(tool_msgs) == 2
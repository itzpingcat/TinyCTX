"""
tests/test_context.py

Tests for Context, HistoryEntry, and the hook pipeline.

Run with:
    pytest tests/
"""
import pytest
from context import (
    Context, HistoryEntry,
    HOOK_PRE_ASSEMBLE, HOOK_FILTER_TURN, HOOK_TRANSFORM_TURN, HOOK_POST_ASSEMBLE,
    ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL, ROLE_SYSTEM,
)
from contracts import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# HistoryEntry constructors
# ---------------------------------------------------------------------------

class TestHistoryEntry:
    def test_user_entry(self):
        e = HistoryEntry.user("hello")
        assert e.role == ROLE_USER
        assert e.content == "hello"
        assert e.tool_calls == []
        assert e.tool_call_id is None

    def test_assistant_entry_no_tools(self):
        e = HistoryEntry.assistant("hi")
        assert e.role == ROLE_ASSISTANT
        assert e.content == "hi"
        assert e.tool_calls == []

    def test_assistant_entry_with_tool_calls(self):
        tc = ToolCall(call_id="abc", tool_name="search", args={"q": "test"})
        e = HistoryEntry.assistant("using search", tool_calls=[tc])
        assert len(e.tool_calls) == 1
        assert e.tool_calls[0]["id"] == "abc"
        assert e.tool_calls[0]["name"] == "search"
        assert e.tool_calls[0]["arguments"] == {"q": "test"}

    def test_tool_result_entry(self):
        result = ToolResult(call_id="abc", tool_name="search", output="found it", is_error=False)
        e = HistoryEntry.tool_result(result)
        assert e.role == ROLE_TOOL
        assert e.content == "found it"
        assert e.tool_call_id == "abc"

    def test_system_entry(self):
        e = HistoryEntry.system("you are helpful")
        assert e.role == ROLE_SYSTEM
        assert e.content == "you are helpful"

    def test_each_entry_gets_unique_id(self):
        a = HistoryEntry.user("a")
        b = HistoryEntry.user("b")
        assert a.id != b.id


# ---------------------------------------------------------------------------
# Context.add() and indexing
# ---------------------------------------------------------------------------

class TestContextAdd:
    def test_add_sets_index(self):
        ctx = Context()
        e0 = ctx.add(HistoryEntry.user("first"))
        e1 = ctx.add(HistoryEntry.user("second"))
        assert e0.index == 0
        assert e1.index == 1

    def test_clear_empties_dialogue_and_state(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("hi"))
        ctx.state["foo"] = "bar"
        ctx.clear()
        assert ctx.dialogue == []
        assert ctx.state == {}


# ---------------------------------------------------------------------------
# Basic assemble() — no hooks, no prompts
# ---------------------------------------------------------------------------

class TestAssembleBasic:
    def test_empty_context_returns_empty(self):
        ctx = Context()
        messages = ctx.assemble()
        assert messages == []

    def test_single_user_message(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("hello"))
        messages = ctx.assemble()
        assert len(messages) == 1
        assert messages[0] == {"role": "user", "content": "hello"}

    def test_user_assistant_exchange(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("hello"))
        ctx.add(HistoryEntry.assistant("hi there"))
        messages = ctx.assemble()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_tool_result_rendered_correctly(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("search for cats"))
        tc = ToolCall(call_id="x1", tool_name="search", args={"q": "cats"})
        ctx.add(HistoryEntry.assistant("searching", tool_calls=[tc]))
        result = ToolResult(call_id="x1", tool_name="search", output="many cats found")
        ctx.add(HistoryEntry.tool_result(result))

        messages = ctx.assemble()
        tool_msg = messages[-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["content"] == "many cats found"
        assert tool_msg["tool_call_id"] == "x1"

    def test_adjacent_user_messages_merged(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("first"))
        ctx.add(HistoryEntry.user("second"))
        messages = ctx.assemble()
        # The two user messages should be merged into one
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert "first" in user_msgs[0]["content"]
        assert "second" in user_msgs[0]["content"]

    def test_adjacent_assistant_messages_merged(self):
        ctx = Context()
        ctx.add(HistoryEntry.assistant("part one"))
        ctx.add(HistoryEntry.assistant("part two"))
        messages = ctx.assemble()
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(asst_msgs) == 1
        assert "part one" in asst_msgs[0]["content"]
        assert "part two" in asst_msgs[0]["content"]

    def test_assistant_with_tool_calls_not_merged(self):
        """Assistant turns with tool_calls must not be merged — they are structurally distinct."""
        tc = ToolCall(call_id="c1", tool_name="fn", args={})
        ctx = Context()
        ctx.add(HistoryEntry.assistant("calling tool", tool_calls=[tc]))
        ctx.add(HistoryEntry.assistant("plain follow-up"))
        messages = ctx.assemble()
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(asst_msgs) == 2


# ---------------------------------------------------------------------------
# Prompt providers
# ---------------------------------------------------------------------------

class TestPromptProviders:
    def test_system_prompt_injected(self):
        ctx = Context()
        ctx.register_prompt("soul", lambda _: "you are helpful")
        messages = ctx.assemble()
        assert messages[0]["role"] == "system"
        assert "you are helpful" in messages[0]["content"]

    def test_none_provider_skipped(self):
        ctx = Context()
        ctx.register_prompt("missing", lambda _: None)
        messages = ctx.assemble()
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert system_msgs == []

    def test_multiple_system_prompts_combined(self):
        ctx = Context()
        ctx.register_prompt("soul",   lambda _: "persona", priority=0)
        ctx.register_prompt("memory", lambda _: "remember this", priority=10)
        messages = ctx.assemble()
        sys_content = messages[0]["content"]
        assert "persona" in sys_content
        assert "remember this" in sys_content

    def test_prompt_priority_order(self):
        ctx = Context()
        ctx.register_prompt("b", lambda _: "second", priority=10)
        ctx.register_prompt("a", lambda _: "first",  priority=0)
        messages = ctx.assemble()
        content = messages[0]["content"]
        assert content.index("first") < content.index("second")

    def test_unregister_prompt(self):
        ctx = Context()
        ctx.register_prompt("soul", lambda _: "persona")
        ctx.unregister_prompt("soul")
        messages = ctx.assemble()
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert system_msgs == []

    def test_raising_provider_skipped_gracefully(self):
        ctx = Context()
        ctx.register_prompt("bad", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
        ctx.add(HistoryEntry.user("hello"))
        # Should not raise, bad provider is skipped
        messages = ctx.assemble()
        assert any(m["role"] == "user" for m in messages)


# ---------------------------------------------------------------------------
# Hook pipeline
# ---------------------------------------------------------------------------

class TestHooks:
    def test_pre_assemble_hook_called(self):
        ctx = Context()
        called = []
        ctx.register_hook(HOOK_PRE_ASSEMBLE, lambda c: called.append(True))
        ctx.add(HistoryEntry.user("hi"))
        ctx.assemble()
        assert called == [True]

    def test_filter_hook_drops_turn(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("keep this"))
        ctx.add(HistoryEntry.assistant("drop this"))

        def drop_assistant(entry, age, c):
            if entry.role == ROLE_ASSISTANT:
                return False

        ctx.register_hook(HOOK_FILTER_TURN, drop_assistant)
        messages = ctx.assemble()
        assert not any(m["role"] == "assistant" for m in messages)

    def test_transform_hook_modifies_content(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("original"))

        def shout(entry, age, c):
            if entry.role == ROLE_USER:
                return HistoryEntry(
                    role=entry.role,
                    content=entry.content.upper(),
                    id=entry.id,
                    index=entry.index,
                )

        ctx.register_hook(HOOK_TRANSFORM_TURN, shout)
        messages = ctx.assemble()
        assert messages[-1]["content"] == "ORIGINAL"

    def test_post_assemble_hook_can_replace_messages(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("hi"))

        def replace(messages, c):
            return [{"role": "user", "content": "replaced"}]

        ctx.register_hook(HOOK_POST_ASSEMBLE, replace)
        messages = ctx.assemble()
        assert messages == [{"role": "user", "content": "replaced"}]

    def test_hook_priority_ordering(self):
        ctx = Context()
        order = []
        ctx.register_hook(HOOK_PRE_ASSEMBLE, lambda c: order.append("b"), priority=10)
        ctx.register_hook(HOOK_PRE_ASSEMBLE, lambda c: order.append("a"), priority=0)
        ctx.assemble()
        assert order == ["a", "b"]

    def test_unregister_hook(self):
        ctx = Context()
        called = []

        def my_hook(c):
            called.append(True)

        ctx.register_hook(HOOK_PRE_ASSEMBLE, my_hook)
        ctx.unregister_hook(HOOK_PRE_ASSEMBLE, my_hook)
        ctx.assemble()
        assert called == []

    def test_transform_returning_none_leaves_entry_unchanged(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("unchanged"))
        ctx.register_hook(HOOK_TRANSFORM_TURN, lambda e, a, c: None)
        messages = ctx.assemble()
        assert messages[-1]["content"] == "unchanged"


# ---------------------------------------------------------------------------
# Token budget enforcement
# ---------------------------------------------------------------------------

class TestTokenBudget:
    def test_old_turns_dropped_when_over_budget(self):
        # Use alternating roles so messages are NOT merged, then blow the budget
        # with a huge assistant turn followed by a short user turn.
        ctx = Context(token_limit=50)
        ctx.add(HistoryEntry.user("hi"))
        ctx.add(HistoryEntry.assistant("A" * 200))  # old, blows budget on its own
        ctx.add(HistoryEntry.user("short"))          # new, must survive

        messages = ctx.assemble()
        contents = [m["content"] for m in messages]
        # The short user message should survive
        assert any("short" in c for c in contents)
        # The giant assistant turn should have been dropped
        assert not any("A" * 200 in c for c in contents)

    def test_tokens_used_stored_in_state(self):
        ctx = Context()
        ctx.add(HistoryEntry.user("hello"))
        ctx.assemble()
        assert "tokens_used" in ctx.state
        assert ctx.state["tokens_used"] > 0

    def test_tool_call_pair_dropped_together(self):
        """
        When a tool-call assistant turn is dropped due to token budget,
        its associated tool result must also be dropped to keep the message
        list valid for the LLM API.
        """
        ctx = Context(token_limit=30)
        tc = ToolCall(call_id="id1", tool_name="fn", args={})
        ctx.add(HistoryEntry.assistant("calling", tool_calls=[tc]))
        result = ToolResult(call_id="id1", tool_name="fn", output="result")
        ctx.add(HistoryEntry.tool_result(result))
        ctx.add(HistoryEntry.user("new question"))

        messages = ctx.assemble()
        # No orphaned tool result should remain
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        asst_tool_msgs = [m for m in messages if m.get("tool_calls")]
        # Either both are present or neither
        assert len(tool_msgs) == len(asst_tool_msgs)
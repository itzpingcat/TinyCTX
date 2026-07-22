"""
tests/test_context.py

Tests for context.py — HistoryEntry, the assembly pipeline (filter/transform
hooks, adjacent-message merge, token-budget trim, post_assemble), and the
.tags / AssembleMeta.invalidated_tags / ctx.state["surviving_tags"] machinery
used to detect when tagged content (e.g. a loaded skill) falls out of context.

Uses a real ConversationDB(":memory:") throughout rather than a hand-rolled
fake, so the DB's actual ancestor-walk / session-state semantics are exercised.

Run with:
    pytest tests/
"""
from __future__ import annotations

import json

import pytest

from TinyCTX.db import ConversationDB
from TinyCTX.context import (
    Context,
    HistoryEntry,
    AssembleMeta,
    ROLE_USER,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_SYSTEM,
    HOOK_PRE_ASSEMBLE,
    HOOK_FILTER_TURN,
    HOOK_TRANSFORM_TURN,
    HOOK_POST_ASSEMBLE,
)
from TinyCTX.contracts import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    d = ConversationDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def ctx(db):
    root = db.get_root()
    return Context(db, tail_node_id=root.id, token_limit=100_000)


def _user(ctx, text, author_id="kamie"):
    return ctx.add(HistoryEntry.user(text, author_id=author_id))


def _assistant(ctx, text="", tool_calls=None):
    return ctx.add(HistoryEntry.assistant(text, tool_calls=tool_calls))


def _tool_result(ctx, call_id, output, tool_name="some_tool"):
    ctx.add_tool_result(ToolResult(call_id=call_id, tool_name=tool_name, output=output))


# ---------------------------------------------------------------------------
# HistoryEntry basics
# ---------------------------------------------------------------------------

class TestHistoryEntry:
    def test_defaults(self):
        e = HistoryEntry(role=ROLE_USER, content="hi")
        assert e.tags == frozenset()
        assert e.tool_calls == []
        assert e.tool_call_id is None
        assert e.id  # auto-generated uuid string

    def test_ids_are_unique(self):
        a = HistoryEntry(role=ROLE_USER, content="a")
        b = HistoryEntry(role=ROLE_USER, content="b")
        assert a.id != b.id

    def test_static_constructors(self):
        u = HistoryEntry.user("hi", author_id="kamie")
        assert u.role == ROLE_USER and u.author_id == "kamie"

        tc = ToolCall.make("foo", {"x": 1})
        a = HistoryEntry.assistant("thinking", tool_calls=[tc])
        assert a.role == ROLE_ASSISTANT
        assert a.tool_calls == [{"id": tc.call_id, "name": "foo", "arguments": {"x": 1}}]

        tr = ToolResult(call_id="c1", tool_name="foo", output="result text")
        t = HistoryEntry.tool_result(tr)
        assert t.role == ROLE_TOOL and t.tool_call_id == "c1" and t.content == "result text"

        s = HistoryEntry.system("be nice")
        assert s.role == ROLE_SYSTEM


# ---------------------------------------------------------------------------
# add() / assemble() round-trip through the real DB
# ---------------------------------------------------------------------------

class TestAddAndAssemble:
    def test_simple_round_trip(self, ctx):
        _user(ctx, "hello there")
        messages, meta = ctx.assemble()
        assert any(m["role"] == "user" and "hello there" in m["content"] for m in messages)
        assert isinstance(meta, AssembleMeta)

    def test_user_prefix_labelling(self, ctx):
        _user(ctx, "hi", author_id="kamie")
        messages, _ = ctx.assemble()
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "kamie" in user_msg["content"]
        assert "hi" in user_msg["content"]

    def test_assistant_tool_call_round_trip(self, ctx):
        tc = ToolCall.make("use_skill", {"name": "foo"})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "# Skill: foo\n\nBody.")
        messages, _ = ctx.assemble()
        assistant_msg = next(m for m in messages if m["role"] == "assistant")
        assert assistant_msg["tool_calls"][0]["function"]["name"] == "use_skill"
        tool_msg = next(m for m in messages if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == tc.call_id
        assert "Body." in tool_msg["content"]

    def test_system_prompt_injected(self, db):
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=100_000)
        ctx.register_prompt("test_system", lambda c: "be helpful", role=ROLE_SYSTEM)
        messages, _ = ctx.assemble()
        assert messages[0]["role"] == "system"
        assert "be helpful" in messages[0]["content"]

    def test_prompt_provider_returning_none_contributes_nothing(self, ctx):
        ctx.register_prompt("noop", lambda c: None, role=ROLE_SYSTEM)
        messages, _ = ctx.assemble()
        assert not any(m["role"] == "system" for m in messages)


# ---------------------------------------------------------------------------
# filter_turn / transform_turn hooks
# ---------------------------------------------------------------------------

class TestFilterAndTransformHooks:
    def test_filter_turn_drops_entry(self, ctx):
        _user(ctx, "keep me")
        dropped_node = _user(ctx, "drop me")

        def drop_it(entry, age, c):
            return entry.id != dropped_node.id

        ctx.register_hook(HOOK_FILTER_TURN, drop_it)
        messages, _ = ctx.assemble()
        contents = [m["content"] for m in messages if m["role"] == "user"]
        assert not any("drop me" in c for c in contents)
        assert any("keep me" in c for c in contents)

    def test_transform_turn_rewrites_content(self, ctx):
        _user(ctx, "original content")

        def rewrite(entry, age, c):
            if entry.role == ROLE_USER:
                from dataclasses import replace
                return replace(entry, content=entry.content.upper())
            return None

        ctx.register_hook(HOOK_TRANSFORM_TURN, rewrite)
        messages, _ = ctx.assemble()
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "ORIGINAL CONTENT" in user_msg["content"]

    def test_hooks_run_in_priority_order(self, ctx):
        _user(ctx, "x")
        calls = []

        def hook_a(entry, age, c):
            calls.append("a")
            return None

        def hook_b(entry, age, c):
            calls.append("b")
            return None

        ctx.register_hook(HOOK_TRANSFORM_TURN, hook_b, priority=10)
        ctx.register_hook(HOOK_TRANSFORM_TURN, hook_a, priority=-10)
        ctx.assemble()
        assert calls == ["a", "b"]

    def test_unregister_hook(self, ctx):
        _user(ctx, "x")
        calls = []

        def hook(entry, age, c):
            calls.append(1)
            return None

        ctx.register_hook(HOOK_TRANSFORM_TURN, hook)
        ctx.assemble()
        assert len(calls) == 1

        ctx.unregister_hook(HOOK_TRANSFORM_TURN, hook)
        ctx.assemble()
        assert len(calls) == 1  # didn't run again


# ---------------------------------------------------------------------------
# Adjacent-message merge
# ---------------------------------------------------------------------------

class TestAdjacentMerge:
    def test_adjacent_user_messages_merge(self, ctx):
        _user(ctx, "first")
        _user(ctx, "second")
        messages, _ = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert "first" in user_msgs[0]["content"] and "second" in user_msgs[0]["content"]

    def test_user_then_assistant_not_merged(self, ctx):
        _user(ctx, "hi")
        _assistant(ctx, "hello back")
        messages, _ = ctx.assemble()
        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant"]

    def test_tool_entries_not_merged_with_assistant(self, ctx):
        tc = ToolCall.make("foo", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "result")
        _assistant(ctx, "done")
        messages, _ = ctx.assemble()
        roles = [m["role"] for m in messages]
        assert roles == ["assistant", "tool", "assistant"]


# ---------------------------------------------------------------------------
# Token-budget trim
# ---------------------------------------------------------------------------

class TestTokenBudgetTrim:
    def test_no_trim_under_budget(self, ctx):
        _user(ctx, "short message")
        messages, meta = ctx.assemble()
        assert meta.was_trimmed is False
        assert meta.tokens_used == meta.tokens_pre_trim

    def test_trims_when_over_budget(self, db):
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=50)
        for i in range(30):
            _user(ctx, f"filler message number {i} " * 10)
        messages, meta = ctx.assemble()
        assert meta.was_trimmed is True
        assert meta.tokens_used <= meta.tokens_pre_trim

    def test_trim_drops_oldest_first(self, db):
        """Alternate user/assistant so adjacent-merge can't collapse everything
        into one blob — otherwise trim can only keep-or-drop the whole thing."""
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=80)
        _user(ctx, "OLDEST_MARKER " * 20)
        _assistant(ctx, "ack")
        for i in range(20):
            _user(ctx, f"filler {i} " * 10)
            _assistant(ctx, f"ack {i}")
        _user(ctx, "NEWEST_MARKER " * 5)
        messages, meta = ctx.assemble()
        assert meta.was_trimmed is True
        all_content = " ".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))
        assert "NEWEST_MARKER" in all_content
        assert "OLDEST_MARKER" not in all_content

    def test_system_message_never_trimmed(self, db):
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=30)
        ctx.register_prompt("sys", lambda c: "SYSTEM_MARKER", role=ROLE_SYSTEM)
        for i in range(20):
            _user(ctx, f"filler {i} " * 10)
        messages, meta = ctx.assemble()
        assert messages[0]["role"] == "system"
        assert "SYSTEM_MARKER" in messages[0]["content"]

    def test_trim_drops_tool_calls_with_their_result(self, db):
        """When an assistant+tool_call pair ages out, both must go together."""
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=60)
        tc = ToolCall.make("old_tool", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "OLD_TOOL_RESULT " * 20)
        for i in range(20):
            _user(ctx, f"filler {i} " * 10)
        messages, meta = ctx.assemble()
        assert meta.was_trimmed is True
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert not any(m.get("tool_call_id") == tc.call_id for m in tool_msgs)


# ---------------------------------------------------------------------------
# post_assemble — now genuinely final (runs after merge + trim + render)
# ---------------------------------------------------------------------------

class TestPostAssemble:
    def test_receives_rendered_dicts(self, ctx):
        _user(ctx, "hi")
        seen = []

        def post(messages, c):
            seen.append(messages)
            return None

        ctx.register_hook(HOOK_POST_ASSEMBLE, post)
        messages, meta = ctx.assemble()
        assert seen[0] == messages
        assert all(isinstance(m, dict) for m in seen[0])

    def test_runs_after_trim(self, db):
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=60)
        for i in range(20):
            _user(ctx, f"filler {i} " * 10)
        seen_lengths = []

        def post(messages, c):
            seen_lengths.append(len(messages))
            return None

        ctx.register_hook(HOOK_POST_ASSEMBLE, post)
        messages, meta = ctx.assemble()
        assert meta.was_trimmed is True
        assert seen_lengths[0] == len(messages)

    def test_can_rewrite_final_content(self, ctx):
        _user(ctx, "my ip is 1.2.3.4")

        def rewrite(messages, c):
            return [{**m, "content": m["content"].replace("1.2.3.4", "REDACTED")}
                    if isinstance(m.get("content"), str) else m for m in messages]

        ctx.register_hook(HOOK_POST_ASSEMBLE, rewrite)
        messages, _ = ctx.assemble()
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "REDACTED" in user_msg["content"]
        assert "1.2.3.4" not in user_msg["content"]


# ---------------------------------------------------------------------------
# Tags / invalidated_tags / surviving_tags
# ---------------------------------------------------------------------------

class TestTags:
    def _tag_tool_entries(self, ctx, tag="mytag"):
        def tagger(entry, age, c):
            if entry.role == ROLE_TOOL:
                from dataclasses import replace
                return replace(entry, tags=entry.tags | {tag})
            return None
        ctx.register_hook(HOOK_TRANSFORM_TURN, tagger, priority=-100)

    def test_tag_survives_when_untouched(self, ctx):
        tc = ToolCall.make("foo", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "result content")
        self._tag_tool_entries(ctx)

        messages, meta = ctx.assemble()
        assert "mytag" not in meta.invalidated_tags
        assert "mytag" in ctx.state["surviving_tags"]

    def test_tag_invalidated_when_filtered_out(self, ctx):
        """filter_turn runs BEFORE transform_turn per entry, so a tag assigned
        by a transform_turn hook can never "count" on an entry that gets
        filtered out the same call (transform never runs on it at all).
        A tagger that needs to survive a filter-based drop must tag during
        pre_assemble instead (which mutates ctx.dialogue before filtering
        starts) — this test demonstrates that pattern working correctly."""
        tc = ToolCall.make("foo", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "result content")

        def tag_in_pre_assemble(c):
            for e in c.dialogue:
                if e.role == ROLE_TOOL:
                    e.tags = e.tags | {"mytag"}

        def drop_tools(entry, age, c):
            return entry.role != ROLE_TOOL

        ctx.register_hook(HOOK_PRE_ASSEMBLE, tag_in_pre_assemble)
        ctx.register_hook(HOOK_FILTER_TURN, drop_tools)
        messages, meta = ctx.assemble()
        assert "mytag" in meta.invalidated_tags
        assert "mytag" not in ctx.state["surviving_tags"]

    def test_tag_invalidated_when_content_destroyed(self, ctx):
        tc = ToolCall.make("foo", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "result content")
        self._tag_tool_entries(ctx)

        def stub_it(entry, age, c):
            if entry.role == ROLE_TOOL:
                from dataclasses import replace
                return replace(entry, content="[stubbed]", tags=frozenset())
            return None

        ctx.register_hook(HOOK_TRANSFORM_TURN, stub_it, priority=100)  # after tagger
        messages, meta = ctx.assemble()
        assert "mytag" in meta.invalidated_tags

    def test_tag_invalidated_by_budget_trim(self, db):
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=80)
        tc = ToolCall.make("foo", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "OLD RESULT " * 20)
        self._tag_tool_entries(ctx)
        for i in range(20):
            _user(ctx, f"filler {i} " * 10)

        messages, meta = ctx.assemble()
        assert meta.was_trimmed is True
        assert "mytag" in meta.invalidated_tags

    def test_tags_union_on_merge(self, ctx):
        def tag_all(entry, age, c):
            from dataclasses import replace
            return replace(entry, tags=entry.tags | {f"tag-{entry.content}"})

        _user(ctx, "a")
        _user(ctx, "b")
        ctx.register_hook(HOOK_TRANSFORM_TURN, tag_all)
        messages, meta = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1  # merged
        assert "tag-a" not in meta.invalidated_tags
        assert "tag-b" not in meta.invalidated_tags

    def test_tag_first_assigned_mid_pipeline_then_destroyed_still_invalidated(self, ctx):
        """A tag that's ADDED by one transform hook and then destructively
        cleared by a LATER hook (in the same pass) must still be reported as
        invalidated — it should never need to have existed before hooks ran."""
        tc = ToolCall.make("foo", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "result")

        def add_tag(entry, age, c):
            if entry.role == ROLE_TOOL:
                from dataclasses import replace
                return replace(entry, tags=entry.tags | {"late_tag"})
            return None

        def destroy(entry, age, c):
            if entry.role == ROLE_TOOL:
                from dataclasses import replace
                return replace(entry, content="[gone]", tags=frozenset())
            return None

        ctx.register_hook(HOOK_TRANSFORM_TURN, add_tag, priority=0)
        ctx.register_hook(HOOK_TRANSFORM_TURN, destroy, priority=10)
        messages, meta = ctx.assemble()
        assert "late_tag" in meta.invalidated_tags

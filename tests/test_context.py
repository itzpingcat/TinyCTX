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
# Thinking (<think>...</think>) → reasoning_content split at render time
#
# agent.py stores reasoning inline as a <think>...</think> prefix on the
# assistant HistoryEntry's content (see agent.py's run() and
# modules/ctx_tools' trim_thinking, which both operate on that stored text).
# _render() peels it back off into its own "reasoning_content" key on the
# OpenAI-compat dict, because that's the field the backend (llama-swap)
# actually expects on replay — mirroring what it sends on the way IN (ai.py
# parses delta["reasoning_content"] off the stream). Content stays whatever
# followed the </think> tag.
# ---------------------------------------------------------------------------

class TestThinkingRender:
    def test_leading_think_block_becomes_reasoning_content(self, ctx):
        _assistant(ctx, "<think>secret reasoning</think>the actual reply")
        messages, _ = ctx.assemble()
        assistant_msg = next(m for m in messages if m["role"] == "assistant")
        assert assistant_msg["reasoning_content"] == "secret reasoning"
        assert assistant_msg["content"] == "the actual reply"
        assert "<think>" not in assistant_msg["content"]

    def test_no_think_block_has_no_reasoning_content_key(self, ctx):
        _assistant(ctx, "plain reply, no thinking")
        messages, _ = ctx.assemble()
        assistant_msg = next(m for m in messages if m["role"] == "assistant")
        assert "reasoning_content" not in assistant_msg
        assert assistant_msg["content"] == "plain reply, no thinking"

    def test_think_block_stripped_by_trim_still_has_no_reasoning_content(self, ctx):
        # If an earlier transform_turn hook (e.g. ctx_tools' cot_strip in
        # "all"/"auto" mode) has already stripped the <think> block out of
        # content before render, there's nothing left to split — no
        # reasoning_content key should appear.
        from dataclasses import replace as _replace
        _assistant(ctx, "<think>hidden</think>reply")

        def strip_it(entry, age, c):
            if entry.role == ROLE_ASSISTANT and "<think>" in (entry.content or ""):
                return _replace(entry, content="reply")
            return None

        ctx.register_hook(HOOK_TRANSFORM_TURN, strip_it)
        messages, _ = ctx.assemble()
        assistant_msg = next(m for m in messages if m["role"] == "assistant")
        assert "reasoning_content" not in assistant_msg
        assert assistant_msg["content"] == "reply"

    def test_reasoning_content_counted_in_token_budget(self, db):
        # A long <think> block must still count against the token budget
        # even though it's rendered into a separate field, not "content" —
        # otherwise the trim loop would systematically undercount assistant
        # turns that carry reasoning.
        root = db.get_root()
        ctx = Context(db, tail_node_id=root.id, token_limit=100_000)
        _user(ctx, "hi")
        _assistant(ctx, "<think>" + ("reasoning " * 2000) + "</think>short reply")
        _, meta_with = ctx.assemble()

        root2 = db.get_root()
        ctx2 = Context(db, tail_node_id=root2.id, token_limit=100_000)
        _user(ctx2, "hi")
        _assistant(ctx2, "short reply")
        _, meta_without = ctx2.assemble()

        assert meta_with.tokens_used > meta_without.tokens_used + 1000


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


# ---------------------------------------------------------------------------
# Deferred (non-system) prompt placement — e.g. equipment_manifest's footer
# ---------------------------------------------------------------------------

class TestDeferredPromptPlacement:
    """
    A role="user" prompt provider (e.g. equipment_manifest's volatile footer)
    must land BEFORE the entire trailing run of consecutive user turns, not
    after them and not spliced in the middle of them — see
    modules/equipment_manifest/__main__.py's module docstring. Because the
    footer is role="user", the adjacent-merge (stage 4) folds it into that
    run as plain text, so "inserted before the run" is what makes the footer
    text land ahead of the user's own message(s) in the merged block.
    """

    def _register_footer(self, ctx, text="FOOTER", priority=99):
        ctx.register_prompt("test_footer", lambda c: text, role=ROLE_USER, priority=priority)

    def test_single_trailing_user_turn(self, ctx):
        _user(ctx, "hello")
        self._register_footer(ctx)
        messages, _ = ctx.assemble()
        user_msgs = [m["content"] for m in messages if m["role"] == ROLE_USER]
        assert len(user_msgs) == 1
        # Footer text precedes the user's own message in the merged block.
        assert user_msgs[0].index("FOOTER") < user_msgs[0].index("hello")

    def test_multiple_consecutive_trailing_user_turns(self, ctx):
        # Simulates a group chat / passive-message batch: several user turns
        # queued up with no assistant reply between them yet.
        _user(ctx, "msg1")
        _user(ctx, "msg2")
        _user(ctx, "msg3")
        self._register_footer(ctx)
        messages, _ = ctx.assemble()
        user_msgs = [m["content"] for m in messages if m["role"] == ROLE_USER]
        assert len(user_msgs) == 1  # all merged into one block
        content = user_msgs[0]
        # Footer must precede ALL of the trailing run, not just the last one.
        assert content.index("FOOTER") < content.index("msg1")
        assert content.index("FOOTER") < content.index("msg2")
        assert content.index("FOOTER") < content.index("msg3")

    def test_lands_before_trailing_users_even_with_tool_calls_after_last_assistant(self, ctx):
        # Regression: anchoring insertion on "the last assistant entry
        # anywhere in history" (instead of "the trailing run of user
        # entries") mis-fires when tool-call/tool-result entries sit between
        # an earlier assistant turn and the true trailing user run — the
        # footer would land right after that assistant turn, ahead of its
        # own tool results, instead of ahead of the user turns that follow.
        tc = ToolCall.make("foo", {})
        _assistant(ctx, "", tool_calls=[tc])
        _tool_result(ctx, tc.call_id, "tool output")
        _user(ctx, "hello")
        self._register_footer(ctx)

        messages, _ = ctx.assemble()
        roles = [m["role"] for m in messages]
        # Tool result must stay directly after its assistant tool-call turn —
        # the footer must not have been spliced between them.
        assistant_i = roles.index(ROLE_ASSISTANT)
        assert roles[assistant_i + 1] == ROLE_TOOL

        user_msgs = [m["content"] for m in messages if m["role"] == ROLE_USER]
        assert len(user_msgs) == 1
        assert user_msgs[0].index("FOOTER") < user_msgs[0].index("hello")

    def test_no_user_turn_yet_appends_after_system(self, ctx):
        # Very first turn in a lane: no user entry at all (e.g. a synthetic
        # trigger). Footer should land right after the system block, not
        # get lost or crash.
        self._register_footer(ctx)
        messages, _ = ctx.assemble()
        assert messages[-1]["role"] == ROLE_USER
        assert "FOOTER" in messages[-1]["content"]

    def test_priority_order_within_deferred_set(self, ctx):
        _user(ctx, "hello")
        ctx.register_prompt("footer_b", lambda c: "SECOND", role=ROLE_USER, priority=2)
        ctx.register_prompt("footer_a", lambda c: "FIRST", role=ROLE_USER, priority=1)
        messages, _ = ctx.assemble()
        content = [m["content"] for m in messages if m["role"] == ROLE_USER][0]
        assert content.index("FIRST") < content.index("SECOND") < content.index("hello")

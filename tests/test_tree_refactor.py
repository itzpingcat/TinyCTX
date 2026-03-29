"""
tests/test_tree_refactor.py

Phase 1 tree-refactor tests.

Covers:
  - ConversationDB (db.py): schema, root node, add/get/ancestors/children
  - Context DB-backed mode: add() writes to DB, assemble() reads from DB,
    edit()/delete()/strip_tool_calls() write-through, set_tail() branching
  - AgentLoop._init_db(): creates workspace/agent.db and cursor file
  - AgentLoop end-to-end: DB nodes written during a turn, cursor persisted

Run with:
    python -m pytest tests/test_tree_refactor.py -v
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from contracts import (
    ContentType, InboundMessage, Platform, SessionKey, UserIdentity,
    ToolCall, ToolResult,
)
from context import (
    Context, HistoryEntry,
    ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL, ROLE_SYSTEM,
)
from db import ConversationDB
from ai import TextDelta, LLMError


# ===========================================================================
# ConversationDB tests
# ===========================================================================

class TestConversationDB:
    @pytest.fixture
    def db(self, tmp_path):
        return ConversationDB(tmp_path / "agent.db")

    def test_root_node_created_on_init(self, db):
        root = db.get_root()
        assert root is not None
        assert root.parent_id is None
        assert root.role == "system"
        assert root.content == ""

    def test_schema_is_idempotent(self, tmp_path):
        """Opening the same DB twice should not raise or duplicate the root."""
        db1 = ConversationDB(tmp_path / "agent.db")
        db2 = ConversationDB(tmp_path / "agent.db")
        root1 = db1.get_root()
        root2 = db2.get_root()
        assert root1.id == root2.id

    def test_add_node_returns_node(self, db):
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="user", content="hello")
        assert node.id is not None
        assert node.parent_id == root.id
        assert node.role == "user"
        assert node.content == "hello"

    def test_get_node_roundtrip(self, db):
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="assistant", content="hi there")
        fetched = db.get_node(node.id)
        assert fetched is not None
        assert fetched.id == node.id
        assert fetched.content == "hi there"

    def test_get_node_missing_returns_none(self, db):
        assert db.get_node("nonexistent-id") is None

    def test_add_node_with_tool_calls(self, db):
        root = db.get_root()
        tc_json = json.dumps([{"id": "c1", "name": "search", "arguments": {}}])
        node = db.add_node(parent_id=root.id, role="assistant", content="", tool_calls=tc_json)
        fetched = db.get_node(node.id)
        assert fetched.tool_calls == tc_json

    def test_add_node_with_tool_call_id(self, db):
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="tool", content="result", tool_call_id="c1")
        fetched = db.get_node(node.id)
        assert fetched.tool_call_id == "c1"

    def test_add_node_with_author_id(self, db):
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="user", content="hi", author_id="@alice:matrix.org")
        fetched = db.get_node(node.id)
        assert fetched.author_id == "@alice:matrix.org"

    def test_get_ancestors_returns_root_to_leaf_order(self, db):
        root = db.get_root()
        n1 = db.add_node(parent_id=root.id, role="user", content="first")
        n2 = db.add_node(parent_id=n1.id, role="assistant", content="second")
        n3 = db.add_node(parent_id=n2.id, role="user", content="third")

        ancestors = db.get_ancestors(n3.id)
        # Should be n1, n2, n3 (root excluded, correct order)
        assert [a.id for a in ancestors] == [n1.id, n2.id, n3.id]

    def test_get_ancestors_excludes_global_root(self, db):
        root = db.get_root()
        n1 = db.add_node(parent_id=root.id, role="user", content="hello")
        ancestors = db.get_ancestors(n1.id)
        assert not any(a.id == root.id for a in ancestors)

    def test_get_ancestors_single_node(self, db):
        root = db.get_root()
        n = db.add_node(parent_id=root.id, role="user", content="solo")
        ancestors = db.get_ancestors(n.id)
        assert len(ancestors) == 1
        assert ancestors[0].id == n.id

    def test_get_children(self, db):
        root = db.get_root()
        a = db.add_node(parent_id=root.id, role="user", content="branch a")
        b = db.add_node(parent_id=root.id, role="user", content="branch b")
        children = db.get_children(root.id)
        child_ids = {c.id for c in children}
        assert a.id in child_ids
        assert b.id in child_ids

    def test_update_node_content(self, db):
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="user", content="original")
        result = db.update_node_content(node.id, "updated")
        assert result is True
        fetched = db.get_node(node.id)
        assert fetched.content == "updated"

    def test_update_node_content_missing_returns_false(self, db):
        result = db.update_node_content("nonexistent", "anything")
        assert result is False

    def test_delete_node(self, db):
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="user", content="to delete")
        result = db.delete_node(node.id)
        assert result is True
        assert db.get_node(node.id) is None

    def test_delete_node_missing_returns_false(self, db):
        result = db.delete_node("nonexistent")
        assert result is False

    def test_linear_ancestry_deep(self, db):
        root = db.get_root()
        current = root.id
        ids = []
        for i in range(10):
            n = db.add_node(parent_id=current, role="user", content=f"msg-{i}")
            ids.append(n.id)
            current = n.id
        ancestors = db.get_ancestors(current)
        assert [a.id for a in ancestors] == ids

    def test_branching_ancestors_independent(self, db):
        """Two branches from the same parent should have independent ancestor chains."""
        root = db.get_root()
        shared = db.add_node(parent_id=root.id, role="user", content="shared")
        branch_a = db.add_node(parent_id=shared.id, role="assistant", content="branch a")
        branch_b = db.add_node(parent_id=shared.id, role="assistant", content="branch b")

        anc_a = db.get_ancestors(branch_a.id)
        anc_b = db.get_ancestors(branch_b.id)

        assert any(a.content == "branch a" for a in anc_a)
        assert not any(a.content == "branch b" for a in anc_a)
        assert any(a.content == "branch b" for a in anc_b)
        assert not any(a.content == "branch a" for a in anc_b)

        # Both branches share the shared node
        assert any(a.id == shared.id for a in anc_a)
        assert any(a.id == shared.id for a in anc_b)


# ===========================================================================
# Context DB-backed mode tests
# ===========================================================================

class TestContextDBBacked:
    @pytest.fixture
    def db(self, tmp_path):
        return ConversationDB(tmp_path / "agent.db")

    @pytest.fixture
    def ctx_db(self, db):
        """A Context wired to a DB, with tail set to a fresh session node."""
        ctx = Context()
        ctx.set_db(db)
        root = db.get_root()
        session_node = db.add_node(parent_id=root.id, role="system", content="session:test")
        ctx.set_tail(session_node.id)
        return ctx, db, session_node

    def test_add_writes_to_db(self, ctx_db):
        ctx, db, _ = ctx_db
        ctx.add(HistoryEntry.user("hello"))
        tail_id = ctx.tail_node_id
        node = db.get_node(tail_id)
        assert node is not None
        assert node.role == "user"
        assert node.content == "hello"

    def test_add_advances_tail(self, ctx_db):
        ctx, db, session_node = ctx_db
        ctx.add(HistoryEntry.user("msg1"))
        tail1 = ctx.tail_node_id
        ctx.add(HistoryEntry.assistant("reply1"))
        tail2 = ctx.tail_node_id
        assert tail1 != tail2
        assert tail1 != session_node.id
        assert tail2 != session_node.id

    def test_add_chains_parent_ids(self, ctx_db):
        ctx, db, session_node = ctx_db
        ctx.add(HistoryEntry.user("first"))
        n1_id = ctx.tail_node_id
        ctx.add(HistoryEntry.assistant("second"))
        n2_id = ctx.tail_node_id
        n2 = db.get_node(n2_id)
        assert n2.parent_id == n1_id

    def test_assemble_reads_from_db(self, ctx_db):
        ctx, db, _ = ctx_db
        ctx.add(HistoryEntry.user("hello from DB"))
        ctx.add(HistoryEntry.assistant("reply from DB"))

        # Clear in-memory dialogue to prove assemble() reloads from DB
        ctx.dialogue.clear()
        messages = ctx.assemble()
        contents = [m["content"] for m in messages]
        assert any("hello from DB" in c for c in contents)
        assert any("reply from DB" in c for c in contents)

    def test_assemble_preserves_correct_order(self, ctx_db):
        ctx, db, _ = ctx_db
        for i in range(5):
            ctx.add(HistoryEntry.user(f"msg-{i}"))
        ctx.dialogue.clear()
        messages = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        # Messages should arrive in insertion order
        merged = user_msgs[0]["content"]  # all merged into one since same role
        for i in range(5):
            assert f"msg-{i}" in merged

    def test_add_list_content_roundtrips(self, ctx_db):
        ctx, db, _ = ctx_db
        content = [{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
        ctx.add(HistoryEntry.user(content))
        ctx.dialogue.clear()
        messages = ctx.assemble()
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert isinstance(user_msgs[0]["content"], list)
        assert user_msgs[0]["content"][0]["text"] == "hello"

    def test_edit_writes_through_to_db(self, ctx_db):
        ctx, db, _ = ctx_db
        entry = ctx.add(HistoryEntry.user("original"))
        ctx.edit(entry.id, "updated")
        node = db.get_node(entry.id)
        assert node.content == "updated"

    def test_delete_removes_from_db(self, ctx_db):
        ctx, db, _ = ctx_db
        entry = ctx.add(HistoryEntry.user("to delete"))
        ctx.delete(entry.id)
        assert db.get_node(entry.id) is None

    def test_tool_call_pair_written_to_db(self, ctx_db):
        ctx, db, _ = ctx_db
        tc = ToolCall(call_id="c1", tool_name="search", args={"q": "cats"})
        asst_entry = ctx.add(HistoryEntry.assistant("calling", tool_calls=[tc]))
        result = ToolResult(call_id="c1", tool_name="search", output="cats found")
        tool_entry = ctx.add(HistoryEntry.tool_result(result))

        asst_node = db.get_node(asst_entry.id)
        tool_node = db.get_node(tool_entry.id)
        assert asst_node is not None
        assert tool_node is not None
        assert tool_node.tool_call_id == "c1"
        assert json.loads(asst_node.tool_calls)[0]["id"] == "c1"

    def test_set_tail_to_different_branch(self, ctx_db):
        """
        After add()-ing on branch A, point the tail at a different branch B
        and verify assemble() returns B's history, not A's.
        """
        ctx, db, session_node = ctx_db
        ctx.add(HistoryEntry.user("branch A message"))
        # Now create an independent branch B from the session node
        branch_b_node = db.add_node(parent_id=session_node.id, role="user", content="branch B message")
        ctx.set_tail(branch_b_node.id)
        ctx.dialogue.clear()
        messages = ctx.assemble()
        contents = [m["content"] for m in messages if m["role"] == "user"]
        assert any("branch B message" in c for c in contents)
        assert not any("branch A message" in c for c in contents)

    def test_clear_does_not_reset_tail(self, ctx_db):
        """clear() clears in-memory state but must not reset _tail_node_id."""
        ctx, db, _ = ctx_db
        ctx.add(HistoryEntry.user("something"))
        tail_before = ctx.tail_node_id
        ctx.clear()
        assert ctx.tail_node_id == tail_before

    def test_context_without_db_uses_in_memory(self):
        """Unwired Context (no DB) still works exactly as before."""
        ctx = Context()
        ctx.add(HistoryEntry.user("in-memory message"))
        ctx.add(HistoryEntry.assistant("in-memory reply"))
        messages = ctx.assemble()
        assert any(m["content"] == "in-memory message" for m in messages if m["role"] == "user")


# ===========================================================================
# AgentLoop DB integration tests
# ===========================================================================

def _make_config(tmp_path):
    cfg = MagicMock()
    cfg.models = {"primary": MagicMock(), "fast": MagicMock()}
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


def _make_msg(text="hello", session_key=None):
    sk = session_key or SessionKey.dm("u1")
    return InboundMessage(
        session_key=sk,
        author=UserIdentity(platform=Platform.CLI, user_id="u1", username="alice"),
        content_type=ContentType.TEXT,
        text=text,
        message_id="msg-1",
        timestamp=time.time(),
    )


def _text_stream(*texts):
    async def _gen(messages, tools=None):
        for t in texts:
            yield TextDelta(text=t)
    return _gen


async def _collect(agent, msg):
    events = []
    async for ev in agent.run(msg):
        events.append(ev)
    return events


@pytest.fixture
def make_agent(tmp_path):
    """
    Factory: returns a fresh AgentLoop wired to tmp_path as workspace.
    LLM is stubbed; modules are not loaded.
    """
    counter = {"n": 0}

    def _factory(stream_fn=None):
        from agent import AgentLoop

        counter["n"] += 1
        sk = SessionKey.dm(f"test-user-{counter['n']}")
        cfg = _make_config(tmp_path)

        primary_llm = MagicMock()
        primary_llm.stream = stream_fn or _text_stream("ok")

        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=primary_llm):
                agent = AgentLoop(session_key=sk, config=cfg)

        agent._models["primary"] = primary_llm
        return agent

    return _factory


class TestAgentLoopDBIntegration:
    def test_agent_db_file_created(self, tmp_path, make_agent):
        make_agent()
        assert (tmp_path / "agent.db").exists()

    def test_cursor_file_created(self, tmp_path, make_agent):
        agent = make_agent()
        safe_key = str(agent.session_key).replace(":", "_")
        cursor_file = tmp_path / "cursors" / safe_key
        assert cursor_file.exists()

    def test_cursor_file_contains_valid_node_id(self, tmp_path, make_agent):
        agent = make_agent()
        safe_key = str(agent.session_key).replace(":", "_")
        cursor_file = tmp_path / "cursors" / safe_key
        node_id = cursor_file.read_text().strip()
        db = ConversationDB(tmp_path / "agent.db")
        node = db.get_node(node_id)
        assert node is not None

    @pytest.mark.asyncio
    async def test_turn_writes_nodes_to_db(self, tmp_path, make_agent):
        agent = make_agent(_text_stream("hello back"))
        await _collect(agent, _make_msg("hello"))
        db = ConversationDB(tmp_path / "agent.db")
        ancestors = db.get_ancestors(agent._tail_node_id)
        roles = [a.role for a in ancestors]
        assert "user" in roles
        assert "assistant" in roles

    @pytest.mark.asyncio
    async def test_cursor_advances_after_turn(self, tmp_path, make_agent):
        agent = make_agent(_text_stream("reply"))
        safe_key = str(agent.session_key).replace(":", "_")
        cursor_file = tmp_path / "cursors" / safe_key
        initial_node_id = cursor_file.read_text().strip()

        await _collect(agent, _make_msg("turn one"))
        final_node_id = cursor_file.read_text().strip()
        assert final_node_id != initial_node_id

    @pytest.mark.asyncio
    async def test_cursor_persists_across_restarts(self, tmp_path):
        """A new AgentLoop for the same session_key should resume from DB."""
        from agent import AgentLoop

        sk = SessionKey.dm("persistent-user")
        cfg = _make_config(tmp_path)

        primary_llm = MagicMock()
        primary_llm.stream = _text_stream("first reply")

        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=primary_llm):
                agent1 = AgentLoop(session_key=sk, config=cfg)
        agent1._models["primary"] = primary_llm
        await _collect(agent1, _make_msg("first message"))
        tail_after_first = agent1._tail_node_id

        # "Restart" — new AgentLoop, same session key
        primary_llm2 = MagicMock()
        primary_llm2.stream = _text_stream("second reply")
        with patch("agent.MODULES_DIR", Path("/nonexistent")):
            with patch("agent._build_llm", return_value=primary_llm2):
                agent2 = AgentLoop(session_key=sk, config=cfg)
        agent2._models["primary"] = primary_llm2

        # agent2 should resume from where agent1 left off
        assert agent2._tail_node_id == tail_after_first

        await _collect(agent2, _make_msg("second message"))

        # Both turns should be in the ancestor chain
        db = ConversationDB(tmp_path / "agent.db")
        ancestors = db.get_ancestors(agent2._tail_node_id)
        contents = [a.content for a in ancestors]
        assert any("first message" in c for c in contents)
        assert any("second message" in c for c in contents)

    @pytest.mark.asyncio
    async def test_reset_clears_memory_not_db(self, tmp_path, make_agent):
        agent = make_agent(_text_stream("reply"))
        await _collect(agent, _make_msg("hello"))
        tail_before_reset = agent._tail_node_id

        agent.reset()

        assert agent.context.dialogue == []
        # DB is intact
        db = ConversationDB(tmp_path / "agent.db")
        assert db.get_node(tail_before_reset) is not None

    @pytest.mark.asyncio
    async def test_multiple_sessions_share_db_independently(self, tmp_path):
        """Two different session keys write to the same DB but have independent cursors."""
        from agent import AgentLoop

        cfg = _make_config(tmp_path)
        llm = MagicMock()

        agents = []
        for i in range(3):
            sk = SessionKey.dm(f"user-{i}")
            llm_i = MagicMock()
            llm_i.stream = _text_stream(f"reply from {i}")
            with patch("agent.MODULES_DIR", Path("/nonexistent")):
                with patch("agent._build_llm", return_value=llm_i):
                    a = AgentLoop(session_key=sk, config=cfg)
            a._models["primary"] = llm_i
            agents.append(a)

        for i, agent in enumerate(agents):
            await _collect(agent, _make_msg(f"message from user {i}"))

        # Each agent should have a distinct tail
        tails = [a._tail_node_id for a in agents]
        assert len(set(tails)) == 3

        # Each agent's ancestor chain should only contain their own messages
        db = ConversationDB(tmp_path / "agent.db")
        for i, agent in enumerate(agents):
            ancestors = db.get_ancestors(agent._tail_node_id)
            contents = " ".join(a.content for a in ancestors)
            assert f"message from user {i}" in contents
            for j in range(3):
                if j != i:
                    assert f"message from user {j}" not in contents

"""
tests/test_db.py

Tests for db.py — ConversationDB, the SQLite-backed conversation tree.
Covers root node creation, add_node, ancestor walk, children/tail lookups,
session state get/set/checkpoint, and flag utilities.

Run with:
    pytest tests/
"""
from __future__ import annotations

import json

import pytest

from TinyCTX.db import ConversationDB


@pytest.fixture
def db():
    d = ConversationDB(":memory:")
    yield d
    d.close()


# ---------------------------------------------------------------------------
# Root node
# ---------------------------------------------------------------------------

class TestRoot:
    def test_get_root_returns_system_node(self, db):
        root = db.get_root()
        assert root.role == "system"
        assert root.content == ""
        assert root.parent_id is None

    def test_get_root_stable_across_calls(self, db):
        a = db.get_root()
        b = db.get_root()
        assert a.id == b.id

    def test_ensure_schema_idempotent(self, db):
        root_before = db.get_root()
        db.ensure_schema()
        root_after = db.get_root()
        assert root_before.id == root_after.id


# ---------------------------------------------------------------------------
# add_node / get_node
# ---------------------------------------------------------------------------

class TestAddNode:
    def test_add_node_basic(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hello")
        assert node.parent_id == root.id
        assert node.role == "user"
        assert node.content == "hello"
        assert node.id

    def test_get_node_round_trip(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        fetched = db.get_node(node.id)
        assert fetched is not None
        assert fetched.content == "hi"

    def test_get_node_missing_returns_none(self, db):
        assert db.get_node("nonexistent") is None

    def test_invalid_role_raises(self, db):
        root = db.get_root()
        with pytest.raises(ValueError):
            db.add_node(root.id, "bogus_role", "x")

    def test_missing_parent_id_raises(self, db):
        with pytest.raises(ValueError):
            db.add_node(None, "user", "x")

    def test_optional_fields(self, db):
        root = db.get_root()
        node = db.add_node(
            root.id, "assistant", "",
            tool_calls='[{"id":"c1"}]',
            tool_call_id=None,
            author_id="agent",
            attachment_paths='["uploads/f.txt"]',
            state_delta='{"k":"v"}',
        )
        assert node.tool_calls == '[{"id":"c1"}]'
        assert node.author_id == "agent"
        assert node.attachment_paths == '["uploads/f.txt"]'
        assert node.state_delta == '{"k":"v"}'


# ---------------------------------------------------------------------------
# get_parent / get_ancestors / get_children / get_tail_nodes
# ---------------------------------------------------------------------------

class TestTreeNavigation:
    def test_get_parent(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        parent = db.get_parent(node.id)
        assert parent.id == root.id

    def test_get_parent_of_root_is_none(self, db):
        root = db.get_root()
        assert db.get_parent(root.id) is None

    def test_get_ancestors_excludes_root(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "first")
        b = db.add_node(a.id, "assistant", "second")
        ancestors = db.get_ancestors(b.id)
        ids = [n.id for n in ancestors]
        assert root.id not in ids
        assert ids == [a.id, b.id]

    def test_get_ancestors_single_node(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "only")
        ancestors = db.get_ancestors(a.id)
        assert [n.id for n in ancestors] == [a.id]

    def test_get_children(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "a")
        b = db.add_node(root.id, "user", "b")
        children = db.get_children(root.id)
        ids = {c.id for c in children}
        assert ids == {a.id, b.id}

    def test_get_children_empty(self, db):
        root = db.get_root()
        leaf = db.add_node(root.id, "user", "leaf")
        assert db.get_children(leaf.id) == []

    def test_get_tail_nodes(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "a")
        b = db.add_node(a.id, "assistant", "b")
        tails = db.get_tail_nodes()
        tail_ids = {n.id for n in tails}
        assert b.id in tail_ids
        assert a.id not in tail_ids


# ---------------------------------------------------------------------------
# update_node_content / update_node_state_delta / delete_node
# ---------------------------------------------------------------------------

class TestUpdates:
    def test_update_node_content(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "old")
        ok = db.update_node_content(node.id, "new")
        assert ok is True
        assert db.get_node(node.id).content == "new"

    def test_update_node_content_missing(self, db):
        assert db.update_node_content("nonexistent", "x") is False

    def test_delete_node(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "gone soon")
        assert db.delete_node(node.id) is True
        assert db.get_node(node.id) is None

    def test_delete_node_missing(self, db):
        assert db.delete_node("nonexistent") is False


# ---------------------------------------------------------------------------
# Session state: get_state / set_state / load_session_state / checkpoint
# ---------------------------------------------------------------------------

class TestSessionState:
    def test_set_state_then_get_state(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        assert db.set_state(node.id, "key1", "value1") is True
        assert db.get_state(node.id, "key1") == "value1"

    def test_get_state_default(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        assert db.get_state(node.id, "missing", "fallback") == "fallback"

    def test_set_state_does_not_clobber_other_keys(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        db.set_state(node.id, "a", 1)
        db.set_state(node.id, "b", 2)
        assert db.get_state(node.id, "a") == 1
        assert db.get_state(node.id, "b") == 2

    def test_set_state_missing_node_returns_false(self, db):
        assert db.set_state("nonexistent", "k", "v") is False

    def test_load_session_state_walks_ancestors_most_recent_wins(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "a")
        db.set_state(a.id, "key", "old")
        b = db.add_node(a.id, "assistant", "b")
        db.set_state(b.id, "key", "new")
        state, depth = db.load_session_state(b.id)
        assert state["key"] == "new"
        assert depth >= 2

    def test_load_session_state_merges_distinct_keys(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "a")
        db.set_state(a.id, "key_a", "va")
        b = db.add_node(a.id, "assistant", "b")
        db.set_state(b.id, "key_b", "vb")
        state, _ = db.load_session_state(b.id)
        assert state["key_a"] == "va"
        assert state["key_b"] == "vb"

    def test_load_session_state_stops_at_checkpoint(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "a")
        db.update_node_state_delta(a.id, json.dumps({"_checkpoint": True, "key": "checkpointed"}))
        b = db.add_node(a.id, "assistant", "b")
        state, depth = db.load_session_state(b.id)
        assert state["key"] == "checkpointed"
        assert "_checkpoint" not in state

    def test_write_checkpoint_if_needed_below_threshold_noop(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "a")
        db.write_checkpoint_if_needed(node.id, {"key": "v"}, depth=1, threshold=10)
        assert db.get_node(node.id).state_delta is None

    def test_write_checkpoint_if_needed_above_threshold_writes(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "a")
        db.write_checkpoint_if_needed(node.id, {"key": "v"}, depth=20, threshold=10)
        delta = json.loads(db.get_node(node.id).state_delta)
        assert delta["_checkpoint"] is True
        assert delta["key"] == "v"


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

class TestFlags:
    def test_add_and_has_flag(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        assert db.has_flag(node.id, "seen") is False
        db.add_flag(node.id, "seen")
        assert db.has_flag(node.id, "seen") is True

    def test_add_flag_idempotent(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        db.add_flag(node.id, "seen")
        db.add_flag(node.id, "seen")
        assert db.get_flags(node.id) == ["seen"]

    def test_remove_flag(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        db.add_flag(node.id, "seen")
        db.remove_flag(node.id, "seen")
        assert db.has_flag(node.id, "seen") is False

    def test_remove_flag_not_present_noop(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "hi")
        db.remove_flag(node.id, "nope")  # should not raise
        assert db.get_flags(node.id) == []

    def test_get_flags_missing_node(self, db):
        assert db.get_flags("nonexistent") == []

    def test_get_nodes_without_flag(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "a")
        b = db.add_node(root.id, "user", "b")
        db.add_flag(a.id, "librarian_visited")
        without = db.get_nodes_without_flag("librarian_visited")
        ids = {n.id for n in without}
        assert b.id in ids
        assert a.id not in ids

    def test_flag_branch_walks_up_until_flagged_ancestor(self, db):
        root = db.get_root()
        a = db.add_node(root.id, "user", "a")
        b = db.add_node(a.id, "assistant", "b")
        c = db.add_node(b.id, "user", "c")
        db.add_flag(a.id, "marker")
        flagged = db.flag_branch(c.id, "marker")
        assert set(flagged) == {c.id, b.id}
        assert db.has_flag(b.id, "marker") is True
        assert db.has_flag(c.id, "marker") is True

    def test_flag_branch_already_flagged_returns_empty(self, db):
        root = db.get_root()
        node = db.add_node(root.id, "user", "a")
        db.add_flag(node.id, "marker")
        assert db.flag_branch(node.id, "marker") == []


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_does_not_raise(self):
        d = ConversationDB(":memory:")
        d.close()  # should not raise

    def test_close_twice_does_not_raise(self):
        d = ConversationDB(":memory:")
        d.close()
        d.close()  # should not raise

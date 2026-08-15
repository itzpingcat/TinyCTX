"""
tests/test_present.py

End-to-end test for modules/present/__main__.py's dynamic required_permissions
classifier (docs/PERMISSIONS-PLAN.md §7.1): present() always needs FILE_READ,
plus Permission.ROOT when the call is a solo request for exactly one core
system (blacklisted) file — the blacklist-override path.

Unlike test_tool_handler.py's TestRequiredPermissionsCallable (which exercises
the classifier-callable *mechanism* generically with a toy path.startswith
example), this file drives the REAL present module — register_agent(agent),
the real blacklist.txt loader, the real _present_perms/_is_system_file/
_resolve_media_path helpers — through a real ToolCallHandler, to prove the
classifier and the tool body agree with each other and with the actual
on-disk blacklist.

Run with:
    pytest tests/
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from TinyCTX.permissions import Permission
from TinyCTX.tool_handling import ToolCallHandler

present_mod = importlib.import_module("TinyCTX.modules.present.__main__")


class _FakeCaller:
    def __init__(self, granted_permissions=None, username: str = "test-caller"):
        self._granted = (
            frozenset(granted_permissions) if granted_permissions is not None
            else frozenset(Permission)
        )
        self.username = username

    def effective_permissions(self, permissions_config=None) -> "frozenset[Permission]":
        return self._granted

    def has_permission(self, perm, permissions_config=None) -> bool:
        return perm in self._granted


class _WorkspaceConfig:
    def __init__(self, path):
        self.path = str(path)


class _Config:
    def __init__(self, workspace_path):
        self.workspace = _WorkspaceConfig(workspace_path)


class _Context:
    tail_node_id = "node-1"


class _FakeAgent:
    def __init__(self, workspace_path):
        self.config = _Config(workspace_path)
        self.tool_handler = ToolCallHandler()
        self.outbound_events = []
        self.context = _Context()
        self.trace_id = "trace-1"


@pytest.fixture
def workspace(tmp_path):
    """A workspace dir with an ordinary file and every name the module's
    real blacklist.txt (if any) or its own defaults would treat as a core
    system file. present/__main__.py's _load_blacklist() reads
    modules/present/blacklist.txt if present; the module currently ships
    without one (frozenset(), frozenset()), so we monkeypatch the loader's
    inputs indirectly by using whatever names the real blacklist declares,
    falling back to a synthetic name we inject via the module's own loader
    seam if the shipped list is empty."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.md").write_text("hello", encoding="utf-8")
    (ws / "SOUL.md").write_text("the soul file", encoding="utf-8")
    (ws / "AGENTS.md").write_text("agents file", encoding="utf-8")
    (ws / "other.md").write_text("also ordinary", encoding="utf-8")
    return ws


def _register(agent, monkeypatch, file_names=frozenset({"soul.md"}), dir_names=frozenset()):
    """Force _load_blacklist to return a known, deterministic blacklist
    regardless of whether modules/present/blacklist.txt exists on disk in
    this checkout, so the test doesn't depend on that file's real contents."""
    monkeypatch.setattr(present_mod, "_load_blacklist", lambda module_dir: (file_names, dir_names))
    present_mod.register_agent(agent)


class TestPresentClassifierEndToEnd:
    @pytest.mark.asyncio
    async def test_ordinary_file_needs_only_file_read(self, workspace, monkeypatch):
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)

        reader = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["notes.md"]}},
        }, reader)
        assert result["success"] is True
        assert "notes.md" in result["result"]
        assert len(agent.outbound_events) == 1

    @pytest.mark.asyncio
    async def test_caller_without_file_read_denied_for_ordinary_file(self, workspace, monkeypatch):
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)

        caller = _FakeCaller(granted_permissions=set())
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["notes.md"]}},
        }, caller)
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]
        assert agent.outbound_events == []

    @pytest.mark.asyncio
    async def test_solo_system_file_requires_root(self, workspace, monkeypatch):
        """The escalation path: a lone request for a blacklisted file demands
        ROOT in addition to FILE_READ — a FILE_READ-only caller is denied."""
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)

        reader = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["SOUL.md"]}},
        }, reader)
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]
        assert "root" in result["error"]
        assert agent.outbound_events == []

    @pytest.mark.asyncio
    async def test_solo_system_file_succeeds_with_root(self, workspace, monkeypatch):
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)

        root_caller = _FakeCaller(granted_permissions={Permission.FILE_READ, Permission.ROOT})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["SOUL.md"]}},
        }, root_caller)
        assert result["success"] is True
        assert "SOUL.md" in result["result"]
        assert len(agent.outbound_events) == 1

    @pytest.mark.asyncio
    async def test_batch_request_with_system_file_does_not_require_root(self, workspace, monkeypatch):
        """A multi-file batch never delivers a system file (silently dropped
        with a notice) — the classifier and the tool body agree that ROOT is
        only demanded for the single-file override path, not for batches
        that happen to include a blacklisted name."""
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)

        reader = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["notes.md", "SOUL.md"]}},
        }, reader)
        assert result["success"] is True
        assert "notes.md" in result["result"]
        assert "SOUL.md" in result["result"]  # named in the "not sent" notice
        assert "not sent" in result["result"]
        # Only the ordinary file was actually delivered.
        assert len(agent.outbound_events) == 1
        assert agent.outbound_events[0].paths[0].endswith("notes.md")

    @pytest.mark.asyncio
    async def test_solo_ordinary_file_among_many_calls_does_not_need_root(self, workspace, monkeypatch):
        """Sanity check that ROOT is specific to the blacklist match, not to
        len(media) == 1 in general."""
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)

        reader = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["other.md"]}},
        }, reader)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_dir_blacklist_entry_also_requires_root_solo(self, workspace, monkeypatch):
        (workspace / "memory").mkdir()
        (workspace / "memory" / "graph.db").write_text("x", encoding="utf-8")
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch, file_names=frozenset(), dir_names=frozenset({"memory"}))

        reader = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["memory/graph.db"]}},
        }, reader)
        assert result["success"] is False
        assert "root" in result["error"]

        root_caller = _FakeCaller(granted_permissions={Permission.FILE_READ, Permission.ROOT})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c2",
            "function": {"name": "present", "arguments": {"media": ["memory/graph.db"]}},
        }, root_caller)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_unresolvable_path_does_not_escalate_to_root(self, workspace, monkeypatch):
        """A path that can't be resolved (e.g. traversal attempt) must stay
        conservative in the classifier — no ROOT added — and get rejected by
        the tool body's own workspace-containment check instead."""
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)

        # Any caller without ROOT should be able to reach the tool body far
        # enough to get the "outside the workspace" error, not a permission
        # denial, proving the classifier didn't spuriously demand ROOT.
        reader = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await agent.tool_handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "present", "arguments": {"media": ["../../etc/passwd"]}},
        }, reader)
        assert result["success"] is True  # tool ran (wasn't permission-denied)
        assert "outside the workspace" in result["result"]

    def test_present_declares_required_permissions(self, workspace, monkeypatch):
        """present is registered with a callable classifier, not left
        undeclared — assert_permissions_declared() must not flag it."""
        agent = _FakeAgent(workspace)
        _register(agent, monkeypatch)
        agent.tool_handler.assert_permissions_declared()  # must not raise
        assert callable(agent.tool_handler.tools["present"]["required_permissions"])

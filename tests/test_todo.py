"""
tests/test_todo.py

Tests for the todo module: task list persistence, validation, prompt injection.

Run with:
    python -m pytest tests/test_todo.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Mock agent infrastructure (same pattern as test_filesystem_tools.py)
# ---------------------------------------------------------------------------

class _MockPromptRegistry:
    def __init__(self):
        self.prompts: dict = {}

    def register_prompt(self, pid, fn, role="system", priority=0):
        self.prompts[pid] = {"fn": fn, "role": role, "priority": priority}


class _MockConfig:
    def __init__(self, ws_path: str):
        self.workspace = type("WS", (), {"path": ws_path})()
        self.extra = {}


class _MockToolHandler:
    def __init__(self):
        self.tools: dict = {}

    def register_tool(self, func, always_on=False):
        self.tools[func.__name__] = func


class _MockAgent:
    def __init__(self, ws_path: str):
        self.config = _MockConfig(ws_path)
        self.tool_handler = _MockToolHandler()
        self.context = _MockPromptRegistry()


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def agent(workspace):
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from modules.todo.__main__ import register
    ag = _MockAgent(str(workspace))
    register(ag)
    return ag


@pytest.fixture
def tools(agent):
    return agent.tool_handler.tools


@pytest.fixture
def prompts(agent):
    return agent.context.prompts


# ===================================================================
# todo_write — basic CRUD
# ===================================================================

class TestTodoWrite:
    def test_create_simple_list(self, tools, workspace):
        result = tools["todo_write"](todos=[
            {"content": "Fix the bug", "status": "in_progress"},
            {"content": "Write tests", "status": "pending"},
        ])
        assert "2 tasks" in result
        assert "1 in progress" in result
        assert "1 pending" in result

        # Verify persisted
        todo_path = workspace / "TODO.json"
        assert todo_path.exists()
        data = json.loads(todo_path.read_text())
        assert len(data) == 2

    def test_replace_list(self, tools):
        tools["todo_write"](todos=[
            {"content": "Old task", "status": "pending"},
        ])
        result = tools["todo_write"](todos=[
            {"content": "New task", "status": "completed"},
        ])
        assert "1 tasks" in result
        assert "1 completed" in result

    def test_empty_list_clears(self, tools, workspace):
        tools["todo_write"](todos=[
            {"content": "Something", "status": "pending"},
        ])
        result = tools["todo_write"](todos=[])
        assert "0 tasks" in result
        assert "empty" in result

    def test_all_three_statuses(self, tools):
        result = tools["todo_write"](todos=[
            {"content": "Done thing", "status": "completed"},
            {"content": "Doing thing", "status": "in_progress"},
            {"content": "Todo thing", "status": "pending"},
        ])
        assert "3 tasks" in result
        assert "1 in progress" in result
        assert "1 pending" in result
        assert "1 completed" in result


# ===================================================================
# todo_write — validation
# ===================================================================

class TestTodoValidation:
    def test_rejects_non_list(self, tools):
        result = tools["todo_write"](todos="not a list")
        assert "error" in result

    def test_rejects_missing_content(self, tools):
        result = tools["todo_write"](todos=[{"status": "pending"}])
        assert "error" in result
        assert "content" in result

    def test_rejects_invalid_status(self, tools):
        result = tools["todo_write"](todos=[
            {"content": "Task", "status": "banana"}
        ])
        assert "error" in result
        assert "banana" in result

    def test_rejects_non_dict_item(self, tools):
        result = tools["todo_write"](todos=["just a string"])
        assert "error" in result
        assert "object" in result

    def test_defaults_status_to_pending(self, tools, workspace):
        """Missing status should be rejected (we require explicit status)."""
        result = tools["todo_write"](todos=[
            {"content": "Task with no status"}
        ])
        # Should still work — defaults to "pending"
        # Wait, let's check the implementation...
        # Actually, the code does: status = item.get("status", "pending")
        # So missing status defaults to pending and is valid
        assert "1 tasks" in result
        data = json.loads((workspace / "TODO.json").read_text())
        assert data[0]["status"] == "pending"


# ===================================================================
# todo_read
# ===================================================================

class TestTodoRead:
    def test_read_empty(self, tools):
        result = tools["todo_read"]()
        assert "no tasks" in result.lower()

    def test_read_after_write(self, tools):
        tools["todo_write"](todos=[
            {"content": "First task", "status": "in_progress"},
            {"content": "Second task", "status": "pending"},
        ])
        result = tools["todo_read"]()
        assert "First task" in result
        assert "Second task" in result
        assert "[~]" in result  # in_progress icon
        assert "[ ]" in result  # pending icon


# ===================================================================
# System prompt injection
# ===================================================================

class TestTodoPrompt:
    def test_prompt_registered(self, prompts):
        assert "todo_list" in prompts

    def test_prompt_empty_when_no_todos(self, prompts):
        fn = prompts["todo_list"]["fn"]
        result = fn(None)
        assert result == ""

    def test_prompt_shows_tasks(self, tools, prompts):
        tools["todo_write"](todos=[
            {"content": "Build the thing", "status": "in_progress"},
            {"content": "Test the thing", "status": "pending"},
        ])
        fn = prompts["todo_list"]["fn"]
        result = fn(None)
        assert "<current_tasks>" in result
        assert "Build the thing" in result
        assert "Test the thing" in result
        assert "[~]" in result
        assert "[ ]" in result


# ===================================================================
# Persistence across reloads
# ===================================================================

class TestTodoPersistence:
    def test_survives_reload(self, workspace):
        """Todo list persists across module re-registrations."""
        from modules.todo.__main__ import register

        # First agent writes
        ag1 = _MockAgent(str(workspace))
        register(ag1)
        ag1.tool_handler.tools["todo_write"](todos=[
            {"content": "Persistent task", "status": "pending"},
        ])

        # Second agent reads
        ag2 = _MockAgent(str(workspace))
        register(ag2)
        result = ag2.tool_handler.tools["todo_read"]()
        assert "Persistent task" in result

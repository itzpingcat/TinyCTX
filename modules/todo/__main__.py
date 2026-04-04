"""
modules/todo/__main__.py

Persistent task checklist for multi-step work. The agent calls todo_write()
to update its task list, and the current list is injected into the system
prompt every turn so it never loses track of what it's doing.

Inspired by Claude Code's TodoWriteTool — adapted for TinyCTX's lightweight
architecture. State lives in workspace/TODO.json.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VALID_STATUSES = {"pending", "in_progress", "completed"}


def _load_todos(path: Path) -> list[dict[str, str]]:
    """Load the todo list from disk. Returns [] on any error."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [
                item for item in data
                if isinstance(item, dict)
                and isinstance(item.get("content"), str)
                and item.get("status") in _VALID_STATUSES
            ]
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("todo: failed to load %s: %s", path, exc)
    return []


def _save_todos(path: Path, todos: list[dict[str, str]]) -> None:
    """Persist the todo list to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")


def _format_todo_list(todos: list[dict[str, str]]) -> str:
    """Render the todo list as a compact, readable block for the system prompt."""
    if not todos:
        return ""
    status_icons = {
        "pending":     "[ ]",
        "in_progress": "[~]",
        "completed":   "[x]",
    }
    lines = ["<current_tasks>"]
    for i, item in enumerate(todos, 1):
        icon = status_icons.get(item.get("status", "pending"), "[ ]")
        content = item.get("content", "???")
        lines.append(f"  {i}. {icon} {content}")
    lines.append("</current_tasks>")
    return "\n".join(lines)


def register(agent) -> None:
    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    todo_path = workspace / "TODO.json"

    # Module-level config
    try:
        from modules.todo import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}
    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        runtime_cfg = agent.config.extra.get("todo", {})
        cfg = {**cfg, **runtime_cfg}

    # ------------------------------------------------------------------
    # System prompt — inject current todo list every turn
    # ------------------------------------------------------------------

    def _todo_prompt(_ctx) -> str:
        todos = _load_todos(todo_path)
        if not todos:
            return ""
        formatted = _format_todo_list(todos)
        return (
            f"{formatted}\n"
            "Update this list with todo_write as you make progress. "
            "Mark tasks in_progress before starting, completed when done."
        )

    agent.context.register_prompt(
        "todo_list",
        _todo_prompt,
        role="system",
        priority=int(cfg.get("prompt_priority", 8)),
    )

    # ------------------------------------------------------------------
    # Tool: todo_write
    # ------------------------------------------------------------------

    def todo_write(todos: list) -> str:
        """Update the session task list. Call this to track multi-step work.
        Each item needs a 'content' (what to do) and 'status' (pending, in_progress, or completed).

        Use this proactively for tasks with 3+ steps. Skip it for trivial single-step requests.

        Args:
            todos: List of task objects. Each must have 'content' (str) and 'status' (pending|in_progress|completed).
        """
        if not isinstance(todos, list):
            return "[error: todos must be a list of {content, status} objects]"

        # Validate and normalize
        clean: list[dict[str, str]] = []
        errors: list[str] = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                errors.append(f"item {i}: must be an object, got {type(item).__name__}")
                continue
            content = item.get("content")
            status = item.get("status", "pending")
            if not content or not isinstance(content, str):
                errors.append(f"item {i}: 'content' must be a non-empty string")
                continue
            if status not in _VALID_STATUSES:
                errors.append(f"item {i}: status '{status}' invalid — use pending, in_progress, or completed")
                continue
            clean.append({"content": content.strip(), "status": status})

        if errors:
            return "[error: invalid items]\n" + "\n".join(errors)

        # Load previous for diff reporting
        old = _load_todos(todo_path)
        _save_todos(todo_path, clean)

        # Build summary
        counts = {s: 0 for s in _VALID_STATUSES}
        for item in clean:
            counts[item["status"]] += 1

        parts = []
        if counts["in_progress"]:
            parts.append(f"{counts['in_progress']} in progress")
        if counts["pending"]:
            parts.append(f"{counts['pending']} pending")
        if counts["completed"]:
            parts.append(f"{counts['completed']} completed")

        summary = ", ".join(parts) if parts else "empty"
        return f"[todo list updated: {len(clean)} tasks — {summary}]"

    def todo_read() -> str:
        """Read the current task list without modifying it.

        Args: (none)
        """
        todos = _load_todos(todo_path)
        if not todos:
            return "[no tasks — use todo_write to create a task list]"
        return _format_todo_list(todos)

    # Register tools
    agent.tool_handler.register_tool(todo_write, always_on=False)
    agent.tool_handler.register_tool(todo_read, always_on=False)

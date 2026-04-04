from __future__ import annotations

import asyncio
import logging
import time
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any

from contracts import AgentError, AgentTextChunk, AgentTextFinal

logger = logging.getLogger(__name__)

_SUBAGENT_BRANCH_PREFIX = "session:subagent:"
_AGENTS: "weakref.WeakSet[object]" = weakref.WeakSet()


@dataclass
class SubagentTask:
    task_id: str
    prompt: str
    parent_tail_node_id: str
    branch_anchor_node_id: str
    branch_tail_node_id: str
    status: str = "running"
    result: str = ""
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    final_tail_node_id: str | None = None
    task: asyncio.Task | None = None


def _snapshot(handle: SubagentTask) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": handle.task_id,
        "status": handle.status,
        "parent_tail_node_id": handle.parent_tail_node_id,
        "branch_anchor_node_id": handle.branch_anchor_node_id,
        "branch_tail_node_id": handle.branch_tail_node_id,
        "started_at": handle.started_at,
    }
    if handle.completed_at is not None:
        payload["completed_at"] = handle.completed_at
    if handle.final_tail_node_id is not None:
        payload["final_tail_node_id"] = handle.final_tail_node_id
    if handle.result:
        payload["result"] = handle.result
    if handle.error:
        payload["error"] = handle.error
    return payload


def _task_registry(agent) -> dict[str, SubagentTask]:
    _AGENTS.add(agent)
    registry = getattr(agent, "_subagent_tasks", None)
    if registry is None:
        registry = {}
        setattr(agent, "_subagent_tasks", registry)
    return registry


def _prune_completed_tasks(agent, completed_ttl_seconds: float, *, now: float | None = None) -> int:
    registry = _task_registry(agent)
    if completed_ttl_seconds <= 0:
        stale_ids = [
            task_id
            for task_id, handle in registry.items()
            if handle.completed_at is not None
        ]
    else:
        now = time.time() if now is None else now
        stale_ids = [
            task_id
            for task_id, handle in registry.items()
            if handle.completed_at is not None and (now - handle.completed_at) >= completed_ttl_seconds
        ]
    for task_id in stale_ids:
        registry.pop(task_id, None)
    return len(stale_ids)


def _running_task_count(agent) -> int:
    registry = _task_registry(agent)
    return sum(
        1
        for handle in registry.values()
        if handle.task is not None and not handle.task.done()
    )


async def spawn_subagent(
    agent,
    prompt: str,
    *,
    max_concurrent: int = 4,
    completed_ttl_seconds: float = 900.0,
) -> dict[str, Any]:
    """Create a detached child branch and start a background AgentLoop on it."""
    pruned = _prune_completed_tasks(agent, completed_ttl_seconds)
    running = _running_task_count(agent)
    if running >= max_concurrent:
        return {
            "status": "error",
            "error": (
                f"Too many subagents are already running ({running}/{max_concurrent}). "
                "Wait for one to finish before spawning another."
            ),
        }

    task_id = str(uuid.uuid4())
    parent_tail_node_id = agent._tail_node_id

    branch_anchor = agent._db.add_node(
        parent_id=parent_tail_node_id,
        role="system",
        content=f"{_SUBAGENT_BRANCH_PREFIX}{task_id}",
    )
    branch_opening = agent._db.add_node(
        parent_id=branch_anchor.id,
        role="user",
        content=prompt,
    )

    handle = SubagentTask(
        task_id=task_id,
        prompt=prompt,
        parent_tail_node_id=parent_tail_node_id,
        branch_anchor_node_id=branch_anchor.id,
        branch_tail_node_id=branch_opening.id,
    )
    handle.task = asyncio.create_task(
        _run_subagent(handle, agent.config),
        name=f"tinyctx-subagent-{task_id[:8]}",
    )
    _task_registry(agent)[task_id] = handle

    payload = _snapshot(handle)
    if pruned:
        payload["pruned_completed_tasks"] = pruned
    payload["message"] = "Subagent started. Use wait_agent(task_id=...) to retrieve the result."
    return payload


async def wait_for_subagent(agent, task_id: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
    """Wait for a spawned subagent to finish, or return its current status."""
    handle = _task_registry(agent).get(task_id)
    if handle is None:
        return {
            "task_id": task_id,
            "status": "missing",
            "error": "Unknown subagent task_id.",
        }

    if handle.task is not None and not handle.task.done():
        try:
            await asyncio.wait_for(asyncio.shield(handle.task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            pass

    payload = _snapshot(handle)
    if payload["status"] == "running":
        payload["message"] = "Subagent still running. Call wait_agent again later."
    return payload


def reset_subagent_tasks() -> None:
    """Best-effort test helper to clear the in-process task registry."""
    for agent in list(_AGENTS):
        registry = getattr(agent, "_subagent_tasks", None)
        if not registry:
            continue
        for handle in list(registry.values()):
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
        registry.clear()


async def _run_subagent(handle: SubagentTask, config) -> None:
    from agent import AgentLoop

    try:
        loop = AgentLoop(
            tail_node_id=handle.branch_tail_node_id,
            config=config,
            is_subagent=True,
        )

        text_parts: list[str] = []
        async for event in loop.run(msg=None):
            if isinstance(event, AgentTextChunk):
                text_parts.append(event.text)
            elif isinstance(event, AgentTextFinal) and event.text:
                text_parts.append(event.text)
            elif isinstance(event, AgentError):
                handle.status = "failed"
                handle.error = event.message
                handle.final_tail_node_id = loop.tail_node_id
                handle.completed_at = time.time()
                return

        handle.status = "completed"
        handle.result = "".join(text_parts).strip()
        handle.final_tail_node_id = loop.tail_node_id
        handle.completed_at = time.time()
    except asyncio.CancelledError:
        handle.status = "cancelled"
        handle.error = "Subagent task was cancelled."
        handle.completed_at = time.time()
        raise
    except Exception as exc:
        logger.exception("[subagents] task %s failed", handle.task_id)
        handle.status = "failed"
        handle.error = str(exc)
        handle.completed_at = time.time()

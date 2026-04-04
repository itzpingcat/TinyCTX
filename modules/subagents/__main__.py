from __future__ import annotations

import json

from subagents import spawn_subagent, wait_for_subagent


def _subagent_prompt(_ctx) -> str:
    return (
        "<subagents>\n"
        "- Use spawn_agent for bounded side tasks that can run independently on a child branch.\n"
        "- Use wait_agent(task_id=...) when you need the spawned subagent's final result.\n"
        "- Do not spawn subagents for trivial work or when you can finish faster in the current turn.\n"
        "</subagents>"
    )


def register(agent) -> None:
    if getattr(agent, "is_subagent", False):
        return

    try:
        from modules.subagents import EXTENSION_META
        defaults: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        defaults = {}

    overrides: dict = {}
    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        overrides = agent.config.extra.get("subagents", {})

    cfg: dict = {**defaults, **overrides}

    max_concurrent = max(1, int(cfg.get("max_concurrent", 4)))
    completed_ttl_seconds = max(0.0, float(cfg.get("completed_ttl_seconds", 900.0)))

    async def spawn_agent(prompt: str) -> str:
        """
        Spawn a detached subagent on a child branch for bounded parallel work.

        Args:
            prompt: The self-contained task for the subagent to execute.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return json.dumps(
                {"status": "error", "error": "spawn_agent requires a non-empty prompt."}
            )
        payload = await spawn_subagent(
            agent,
            prompt,
            max_concurrent=max_concurrent,
            completed_ttl_seconds=completed_ttl_seconds,
        )
        return json.dumps(payload, ensure_ascii=False)

    async def wait_agent(task_id: str, timeout_seconds: float = 60.0) -> str:
        """
        Wait for a spawned subagent to finish and return its status or result.

        Args:
            task_id: The task_id returned by spawn_agent.
            timeout_seconds: Maximum time to wait before returning the current status. Use 0 to poll.
        """
        if timeout_seconds < 0:
            return json.dumps(
                {"status": "error", "error": "timeout_seconds must be greater than or equal to 0."}
            )
        payload = await wait_for_subagent(
            agent,
            task_id,
            timeout_seconds=timeout_seconds,
        )
        return json.dumps(payload, ensure_ascii=False)

    agent.tool_handler.register_tool(spawn_agent, always_on=True)
    agent.tool_handler.register_tool(wait_agent, always_on=True)
    agent.context.register_prompt(
        "subagents",
        _subagent_prompt,
        role="system",
        priority=int(cfg.get("prompt_priority", 13)),
    )

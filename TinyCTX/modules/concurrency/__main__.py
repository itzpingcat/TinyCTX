"""
modules/concurrency/__main__.py — Concurrent Forks (docs/PLAN.md).

register_runtime(runtime) — once at startup: capture the Runtime singleton.

register_agent(cycle) — per AgentCycle:
  1. Register the "running_forks" roster prompt provider (role=user, so it
     re-renders every assemble() outside the cached prefix — same reason
     equipment_manifest_footer exists).
  2. Register spawn_fork and nudge_fork tools.

Both tools resolve peers through agent.run.session_key — neither can reach
outside the session (§10.1). This module absorbs everything modules/subagents
did; spawn_fork replaces spawn_agent/wait_agent — there is no wait_agent
equivalent, and nothing replaces it (§9.1).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Module-global, set once by register_runtime — matches modules/sysops.
_runtime = None


def register_runtime(runtime) -> None:
    global _runtime
    _runtime = runtime
    logger.info("[concurrency] registered")


# ---------------------------------------------------------------------------
# Roster prompt provider
# ---------------------------------------------------------------------------

def _format_run_line(run) -> str:
    return f"- fork {run.id[:8]}: {run.intent!r}"


def _running_forks_provider(cycle):
    def provider(_ctx):
        if _runtime is None or cycle.run is None:
            return None
        peers = [
            r for r in _runtime.runs_in_session(cycle.run.session_key)
            if r.id != cycle.run.id and r.status == "running"
        ]
        if not peers:
            return None
        lines = ["<running_forks>"] + [_format_run_line(r) for r in peers] + ["</running_forks>"]
        return "\n".join(lines)
    return provider


# ---------------------------------------------------------------------------
# register_agent
# ---------------------------------------------------------------------------

def register_agent(agent) -> None:
    if _runtime is None:
        logger.error("[concurrency] register_agent before register_runtime — skipping")
        return
    if agent.run is None:
        # Defensive: every AgentCycle.run() call passes a Run handle now, but
        # skip cleanly rather than crashing if something odd wires this in
        # without one.
        return

    try:
        from TinyCTX.modules.concurrency import EXTENSION_META
        priority = int(EXTENSION_META.get("default_config", {}).get("roster_priority", 13))
    except ImportError:
        priority = 13

    agent.context.register_prompt(
        "running_forks",
        _running_forks_provider(agent),
        role="user",
        priority=priority,
    )

    async def spawn_fork(prompt: str) -> str:
        """
        Start a concurrent fork of yourself on a fresh branch off your current
        head, with prompt as its triggering message. Returns the new run_id.

        The fork runs independently and concurrently with you — this call does
        not wait for it. Its completion digest reaches you automatically (via
        your inbox, or via the trunk if you finish first) once it's done; there
        is no wait_fork.

        Args:
            prompt: The self-contained task for the fork to execute.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return json.dumps({"status": "error", "error": "spawn_fork requires a non-empty prompt."})

        # start_run writes the branch node itself, under the session lock (§6).
        run = await _runtime.start_run(
            agent.run.session_key,
            prompt,
            agent.context.tail_node_id,
            agent.caller,
            parent=agent.run,
        )
        return json.dumps({"status": "ok", "run_id": run.id})

    async def nudge_fork(run_id: str, message: str) -> str:
        """
        Send an advisory, one-way message to a peer fork running in this same
        session. There is no ack — the target may or may not comply, and the
        only feedback is its eventual completion digest.

        Args:
            run_id: The target fork's run_id (see <running_forks> in your context).
            message: Free-text advisory for the target fork.
        """
        message = (message or "").strip()
        if not message:
            return json.dumps({"status": "error", "error": "nudge_fork requires a non-empty message."})
        ok = _runtime.nudge(run_id, agent.run, message)
        if not ok:
            return json.dumps({"status": "error", "error": "That fork already completed (or is unknown)."})
        return json.dumps({"status": "ok"})

    agent.tool_handler.register_tool(spawn_fork, always_on=False, min_permission=50)
    agent.tool_handler.register_tool(nudge_fork, always_on=False, min_permission=50)

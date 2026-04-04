"""
modules/heartbeat/__main__.py

Runs periodic agent turns on a configurable interval, isolated on their own
DB branch — never polluting the user's conversation thread.

Branch strategy (configured via "branch_from"):
  "root"    — branch off the global DB root, fully independent of the user session
  "session" — branch off the current tail of the agent's own session at the time
              heartbeat starts (inherits history up to that point, then diverges)

This mirrors how cron jobs work: a session-init node is created once and stored
as the cursor. All subsequent heartbeat turns append to that same branch.

Reply handling:
  - "HEARTBEAT_OK" at start or end → silently dropped
    (if remaining content is ≤ ack_max_chars).
  - Any other reply → printed as a heartbeat alert, then the agent is
    re-prompted: "Continue the task, or reply HEARTBEAT_OK when done."
  - This continuation loop runs up to max_continuations times before giving up.
  - Errors are logged; the background task continues normally.

HEARTBEAT.md in the workspace is read by the agent via the normal filesystem
tools — this module doesn't inject it directly, the prompt tells the agent to.

Active hours: if configured, ticks outside the window are skipped.
The task still sleeps its normal interval; it just does nothing on waking
outside the allowed window.

Convention: register(agent) — no imports from gateway or bridges.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path

from contracts import (
    InboundMessage, ContentType,
    UserIdentity, Platform,
)

logger = logging.getLogger(__name__)

_HEARTBEAT_USER_ID = "heartbeat-system"
_HEARTBEAT_AUTHOR  = UserIdentity(
    platform=Platform.CRON,
    user_id=_HEARTBEAT_USER_ID,
    username="heartbeat",
)
_TOKEN = "HEARTBEAT_OK"


# ---------------------------------------------------------------------------
# Cursor bootstrap
# ---------------------------------------------------------------------------

def _get_or_create_cursor(agent, branch_from: str) -> tuple[str, str]:
    """
    Return (lane_node_id, tail_node_id) for the heartbeat branch.

    lane_node_id — the stable original branch root; never changes; used as
                   the lane dict key and cursor-handler registration key,
                   because router._dispatch_event always dispatches using
                   event.lane_node_id (the immutable lane origin).
    tail_node_id — the advancing DB tail; passed in InboundMessage so turns
                   append to the correct leaf node.

    The lane node is created once; both ids are stored on the agent instance
    so subsequent calls just return the cached values.
    """
    attr_lane = "_heartbeat_lane_node_id"
    attr_tail = "_heartbeat_cursor_node_id"
    if getattr(agent, attr_lane, None):
        return getattr(agent, attr_lane), getattr(agent, attr_tail)

    from db import ConversationDB
    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    db        = ConversationDB(workspace / "agent.db")

    if branch_from == "session":
        # Branch off the live session tail — the heartbeat "knows" what the
        # user has discussed so far, but its turns won't appear in their thread.
        parent_id = agent._tail_node_id
    else:
        # Branch off the global root — fully isolated, no user history.
        parent_id = db.get_root().id

    node = db.add_node(
        parent_id=parent_id,
        role="system",
        content="session:heartbeat",
    )
    setattr(agent, attr_lane, node.id)
    setattr(agent, attr_tail, node.id)
    logger.info(
        "[heartbeat] created branch cursor %s (branch_from=%s, parent=%s)",
        node.id, branch_from, parent_id,
    )
    return node.id, node.id


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

def register(agent) -> None:
    try:
        from modules.heartbeat import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    every_minutes = int(cfg.get("every_minutes", 30))
    if every_minutes <= 0:
        logger.info("[heartbeat] disabled (every_minutes=0)")
        return

    prompt              = cfg.get("prompt", "If nothing needs attention, reply HEARTBEAT_OK.")
    continuation_prompt = cfg.get("continuation_prompt", "Continue the task, or reply HEARTBEAT_OK when you are done.")
    ack_max             = int(cfg.get("ack_max_chars", 300))
    max_continuations   = int(cfg.get("max_continuations", 5))
    active_hours        = cfg.get("active_hours", None)
    branch_from         = cfg.get("branch_from", "root")   # "root" | "session"
    interval_secs       = every_minutes * 60

    # Bootstrap the branch cursor now (while the agent's tail_node_id is
    # fresh and before any user turns advance it further).
    lane_node_id, tail_node_id = _get_or_create_cursor(agent, branch_from)

    # Note: we'd like to stash agent on gateway here for /debug heartbeat, but
    # agent.gateway is set by Lane.__post_init__ *after* register() returns, so
    # it's None at this point.  The stash is done lazily in _run_turn() instead.

    task = asyncio.get_event_loop().create_task(
        _heartbeat_loop(
            agent, lane_node_id, tail_node_id, interval_secs,
            prompt, continuation_prompt,
            ack_max, max_continuations,
            active_hours,
        ),
        name=f"heartbeat:{lane_node_id}",
    )

    _patch_reset(agent, task)

    logger.info(
        "[heartbeat] started — every %dm, lane=%s, branch_from=%s, active_hours=%s",
        every_minutes, lane_node_id, branch_from, active_hours,
    )


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def _heartbeat_loop(
    agent,
    lane_node_id: str,
    tail_node_id: str,
    interval_secs: int,
    prompt: str,
    continuation_prompt: str,
    ack_max: int,
    max_continuations: int,
    active_hours: dict | None,
) -> None:
    # Wait one full interval before the first tick so startup isn't noisy.
    await asyncio.sleep(interval_secs)

    while True:
        try:
            if _in_active_window(active_hours):
                tail_node_id = await _tick(
                    agent, lane_node_id, tail_node_id,
                    prompt, continuation_prompt,
                    ack_max, max_continuations,
                )
                setattr(agent, "_heartbeat_cursor_node_id", tail_node_id)
            else:
                logger.debug("[heartbeat] outside active hours — skipping tick")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[heartbeat] unhandled error during tick")

        await asyncio.sleep(interval_secs)


# ---------------------------------------------------------------------------
# Single tick — push through gateway, collect reply, continuation loop
# ---------------------------------------------------------------------------

async def _tick(
    agent,
    lane_node_id: str,
    tail_node_id: str,
    prompt: str,
    continuation_prompt: str,
    ack_max: int,
    max_continuations: int,
) -> str:
    """Run one heartbeat tick. Returns the updated tail_node_id."""
    logger.debug("[heartbeat] tick start (lane=%s, tail=%s)", lane_node_id, tail_node_id)

    reply, tail_node_id = await _run_turn(agent, lane_node_id, tail_node_id, prompt)

    is_ok, alert = _parse_reply(reply, ack_max)
    if is_ok:
        logger.debug("[heartbeat] OK on initial turn")
        return tail_node_id

    _emit_alert(alert)

    for turn in range(1, max_continuations + 1):
        logger.debug("[heartbeat] continuation turn %d/%d", turn, max_continuations)
        reply, tail_node_id = await _run_turn(agent, lane_node_id, tail_node_id, continuation_prompt)

        is_ok, alert = _parse_reply(reply, ack_max)
        if is_ok:
            logger.debug("[heartbeat] OK after %d continuation turn(s)", turn)
            return tail_node_id

        _emit_alert(alert)

    logger.warning(
        "[heartbeat] max_continuations (%d) reached without HEARTBEAT_OK — giving up",
        max_continuations,
    )
    return tail_node_id


async def _run_turn(
    agent,
    lane_node_id: str,
    tail_node_id: str,
    text: str,
) -> tuple[str, str]:
    """
    Push a heartbeat message through the gateway on the heartbeat branch.

    lane_node_id — stable lane key, used for cursor-handler registration.
                   router._dispatch_event always dispatches via event.lane_node_id,
                   which is set to the lane's original node_id and never advances.
                   Registering the handler under this key ensures events are
                   delivered correctly even after the tail cursor has moved forward.

    tail_node_id — the current DB leaf; passed as InboundMessage.tail_node_id so
                   the agent loop appends new nodes to the correct branch position.

    Returns (reply_text, new_tail_node_id).
    """
    from contracts import AgentTextChunk, AgentTextFinal, AgentError

    gateway = getattr(agent, "gateway", None)
    if gateway is None:
        logger.error("[heartbeat] agent.gateway not set — cannot run tick")
        return "", tail_node_id

    # Lazily stash the agent ref on the gateway the first time a turn runs.
    # We can't do this in register() because lane.gateway is set by Lane.__post_init__
    # after AgentLoop.__init__ (and thus _load_modules / register) has returned.
    if not hasattr(gateway, "_heartbeat_agent"):
        gateway._heartbeat_agent = agent

    msg = InboundMessage(
        tail_node_id=tail_node_id,
        author=_HEARTBEAT_AUTHOR,
        content_type=ContentType.TEXT,
        text=text,
        message_id=f"heartbeat-{int(time.time_ns())}",
        timestamp=time.time(),
    )

    parts: list[str] = []
    reply_event      = asyncio.Event()

    async def _collect(event) -> None:
        if isinstance(event, AgentTextChunk):
            parts.append(event.text)
        elif isinstance(event, AgentTextFinal):
            if event.text:
                parts.append(event.text)
            reply_event.set()
        elif isinstance(event, AgentError):
            parts.append(event.message)
            reply_event.set()

    # Register under lane_node_id — the stable key that _dispatch_event uses.
    gateway.register_cursor_handler(lane_node_id, _collect)
    try:
        await gateway.push(msg)
        await asyncio.wait_for(reply_event.wait(), timeout=120)
    except asyncio.TimeoutError:
        gateway.abort_generation(lane_node_id)
        logger.error("[heartbeat] turn timed out after 120s")
    finally:
        gateway.unregister_cursor_handler(lane_node_id)

    return "".join(parts).strip(), tail_node_id


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

def _parse_reply(reply: str, ack_max: int) -> tuple[bool, str]:
    """
    Strip HEARTBEAT_OK from the start or end of the reply.
    is_ok=True when the remainder is ≤ ack_max chars.
    """
    text = reply
    if text.startswith(_TOKEN):
        text = text[len(_TOKEN):].lstrip(" \n\r")
    elif text.endswith(_TOKEN):
        text = text[: -len(_TOKEN)].rstrip(" \n\r")
    return len(text) <= ack_max, text


def _emit_alert(text: str) -> None:
    print(f"\n[HEARTBEAT ALERT]\n{text}\n")
    logger.info("[heartbeat] alert delivered (%d chars)", len(text))


# ---------------------------------------------------------------------------
# Active hours
# ---------------------------------------------------------------------------

def _parse_hhmm(s: str) -> dtime:
    h, m = s.strip().split(":")
    return dtime(int(h), int(m))


def _in_active_window(active_hours: dict | None) -> bool:
    if not active_hours:
        return True
    try:
        start = _parse_hhmm(active_hours["start"])
        end_  = _parse_hhmm(active_hours["end"])
    except (KeyError, ValueError):
        logger.warning("[heartbeat] invalid active_hours config — running anyway")
        return True
    if start == end_:
        return False
    now = datetime.now().time().replace(second=0, microsecond=0)
    if start < end_:
        return start <= now < end_
    return now >= start or now < end_


# ---------------------------------------------------------------------------
# Reset hook
# ---------------------------------------------------------------------------

def _patch_reset(agent, task: asyncio.Task) -> None:
    """Cancel the heartbeat task when agent.reset() is called."""
    original_reset = agent.reset

    def patched_reset():
        original_reset()
        if not task.done():
            task.cancel()
            logger.info("[heartbeat] task cancelled on session reset")

    agent.reset = patched_reset

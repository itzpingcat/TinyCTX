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

def _get_or_create_cursor(agent, branch_from: str) -> str:
    """
    Return the node_id for the heartbeat branch cursor.

    branch_from == "root":    child of the global DB root (fully isolated)
    branch_from == "session": child of the agent's current tail at startup
                               (inherits history up to this point, then diverges)

    The node is created once; the node_id is stored on the agent instance so
    subsequent calls just return the cached value.
    """
    attr = "_heartbeat_cursor_node_id"
    if getattr(agent, attr, None):
        return getattr(agent, attr)

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
    setattr(agent, attr, node.id)
    logger.info(
        "[heartbeat] created branch cursor %s (branch_from=%s, parent=%s)",
        node.id, branch_from, parent_id,
    )
    return node.id


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
    cursor_node_id = _get_or_create_cursor(agent, branch_from)

    task = asyncio.get_event_loop().create_task(
        _heartbeat_loop(
            agent, cursor_node_id, interval_secs,
            prompt, continuation_prompt,
            ack_max, max_continuations,
            active_hours,
        ),
        name=f"heartbeat:{cursor_node_id}",
    )

    _patch_reset(agent, task)

    logger.info(
        "[heartbeat] started — every %dm, cursor=%s, branch_from=%s, active_hours=%s",
        every_minutes, cursor_node_id, branch_from, active_hours,
    )


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def _heartbeat_loop(
    agent,
    cursor_node_id: str,
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
                await _tick(
                    agent, cursor_node_id,
                    prompt, continuation_prompt,
                    ack_max, max_continuations,
                )
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
    cursor_node_id: str,
    prompt: str,
    continuation_prompt: str,
    ack_max: int,
    max_continuations: int,
) -> None:
    logger.debug("[heartbeat] tick start (cursor=%s)", cursor_node_id)

    reply, new_cursor = await _run_turn(agent, cursor_node_id, prompt)
    if new_cursor:
        cursor_node_id = new_cursor

    is_ok, alert = _parse_reply(reply, ack_max)
    if is_ok:
        logger.debug("[heartbeat] OK on initial turn")
        return

    _emit_alert(alert)

    for turn in range(1, max_continuations + 1):
        logger.debug("[heartbeat] continuation turn %d/%d", turn, max_continuations)
        reply, new_cursor = await _run_turn(agent, cursor_node_id, continuation_prompt)
        if new_cursor:
            cursor_node_id = new_cursor

        is_ok, alert = _parse_reply(reply, ack_max)
        if is_ok:
            logger.debug("[heartbeat] OK after %d continuation turn(s)", turn)
            return

        _emit_alert(alert)

    logger.warning(
        "[heartbeat] max_continuations (%d) reached without HEARTBEAT_OK — giving up",
        max_continuations,
    )


async def _run_turn(agent, cursor_node_id: str, text: str) -> tuple[str, str | None]:
    """
    Push a heartbeat message through the gateway on the heartbeat branch.

    Returns (reply_text, updated_cursor_node_id | None).
    The cursor advances as the AgentLoop writes new DB nodes; we read the
    new tail off the lane after the turn completes so the next tick resumes
    from the correct leaf.
    """
    from contracts import AgentTextChunk, AgentTextFinal, AgentError

    gateway = getattr(agent, "gateway", None)
    if gateway is None:
        logger.error("[heartbeat] agent.gateway not set — cannot run tick")
        return "", None

    msg = InboundMessage(
        tail_node_id=cursor_node_id,
        author=_HEARTBEAT_AUTHOR,
        content_type=ContentType.TEXT,
        text=text,
        message_id=f"heartbeat-{int(time.time_ns())}",
        timestamp=time.time(),
    )

    parts: list[str]  = []
    reply_event       = asyncio.Event()

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

    gateway.register_cursor_handler(cursor_node_id, _collect)
    try:
        await gateway.push(msg)
        await asyncio.wait_for(reply_event.wait(), timeout=120)
    except asyncio.TimeoutError:
        logger.error("[heartbeat] turn timed out after 120s")
    finally:
        gateway.unregister_cursor_handler(cursor_node_id)

    # Advance cursor to the lane's current tail so the next turn continues
    # from the correct leaf rather than re-sending to the old node.
    new_cursor: str | None = None
    lane = gateway._lane_router._lanes.get(cursor_node_id)
    if lane and lane.loop._tail_node_id != cursor_node_id:
        new_cursor = lane.loop._tail_node_id
        # Cache the updated cursor on the agent so _get_or_create_cursor
        # returns the right value if re-called (e.g. after reset).
        setattr(agent, "_heartbeat_cursor_node_id", new_cursor)

    return "".join(parts).strip(), new_cursor


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

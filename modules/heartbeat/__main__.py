"""
modules/heartbeat/__main__.py

Runs periodic agent turns on a configurable interval, isolated on their own
DB branch — never polluting the user's conversation thread.

Branch strategy (configured via "branch_from"):
  "root"    — branch off the global DB root, fully independent of the user session
  "session" — branch off the current tail of the agent's own session at the time
              heartbeat starts (inherits history up to that point, then diverges)

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

Slash command:
  /heartbeat run  — fire one tick immediately (replaces /debug heartbeat)

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
    Both ids are stored on the agent instance so subsequent calls return cached values.
    """
    attr_lane = "_heartbeat_lane_node_id"
    attr_tail = "_heartbeat_cursor_node_id"
    if getattr(agent, attr_lane, None):
        return getattr(agent, attr_lane), getattr(agent, attr_tail)

    from db import ConversationDB
    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    db        = ConversationDB(workspace / "agent.db")

    if branch_from == "session":
        parent_id = agent._tail_node_id
    else:
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

    prompt              = cfg.get("prompt", "If nothing needs attention, reply HEARTBEAT_OK.")
    continuation_prompt = cfg.get("continuation_prompt", "Continue the task, or reply HEARTBEAT_OK when you are done.")
    ack_max             = int(cfg.get("ack_max_chars", 300))
    max_continuations   = int(cfg.get("max_continuations", 5))
    active_hours        = cfg.get("active_hours", None)
    branch_from         = cfg.get("branch_from", "root")
    interval_secs       = every_minutes * 60

    # Bootstrap the branch cursor while agent's tail_node_id is fresh.
    lane_node_id, tail_node_id = _get_or_create_cursor(agent, branch_from)

    # ------------------------------------------------------------------
    # Register /heartbeat run command via the shared command registry.
    # agent.commands is set by Lane.__post_init__ from router.commands;
    # it's always available at register() time because Lane sets it before
    # calling _load_modules().
    # ------------------------------------------------------------------
    registry = getattr(agent, "commands", None)
    if registry is not None:
        async def _cmd_run(args: list[str], context: dict) -> None:
            """Fire one heartbeat tick immediately."""
            console = context.get("console")
            c       = context.get("theme_c", lambda k: "")

            current_tail = getattr(agent, "_heartbeat_cursor_node_id", tail_node_id)
            current_lane = getattr(agent, "_heartbeat_lane_node_id", lane_node_id)

            if console:
                console.print(f"[{c('tool_call')}]  ⏱  firing heartbeat tick (lane={current_lane[:8]}…)[/{c('tool_call')}]")

            try:
                new_tail = await _tick(
                    agent, current_lane, current_tail,
                    prompt, continuation_prompt,
                    ack_max, max_continuations,
                )
                setattr(agent, "_heartbeat_cursor_node_id", new_tail)
                if console:
                    console.print(f"[{c('tool_ok')}]  ✓  heartbeat tick complete[/{c('tool_ok')}]")
            except Exception as exc:
                logger.exception("[heartbeat] /heartbeat run raised")
                if console:
                    console.print(f"[{c('error')}]  ✗  heartbeat tick raised: {exc}[/{c('error')}]")

        registry.register(
            "heartbeat", "run", _cmd_run,
            help="Fire one heartbeat tick immediately",
        )
        logger.debug("[heartbeat] registered /heartbeat run command")

    # Start the periodic background loop (skip if disabled).
    if every_minutes <= 0:
        logger.info("[heartbeat] periodic ticks disabled (every_minutes=0)")
        return

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
# Single tick
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
    """Push a heartbeat message through the gateway. Returns (reply_text, tail_node_id)."""
    from contracts import AgentTextChunk, AgentTextFinal, AgentError

    gateway = getattr(agent, "gateway", None)
    if gateway is None:
        logger.error("[heartbeat] agent.gateway not set — cannot run tick")
        return "", tail_node_id

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
    text    = reply
    matched = False

    if text == "":
        return True, ""

    if text.startswith(_TOKEN):
        text    = text[len(_TOKEN):].lstrip(" \n\r")
        matched = True
    elif text.endswith(_TOKEN):
        text    = text[: -len(_TOKEN)].rstrip(" \n\r")
        matched = True
    return matched and len(text) <= ack_max, text


def _emit_alert(text: str) -> None:
    logger.warning("[HEARTBEAT ALERT]\n%s", text)


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

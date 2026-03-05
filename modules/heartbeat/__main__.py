"""
modules/heartbeat/__main__.py

Runs periodic agent turns on a configurable interval.
Each tick injects a synthetic InboundMessage directly into AgentLoop.run(),
bypassing the gateway (no platform routing needed).

Reply handling:
  - "HEARTBEAT_OK" at start or end → silently dropped
    (if remaining content is ≤ ack_max_chars).
  - Any other reply → printed as a heartbeat alert, then the agent is
    re-prompted: "Continue the task, or reply HEARTBEAT_OK when done."
  - This continuation loop runs up to max_continuations times before giving up.
  - Errors are logged and the background task continues normally.

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

from contracts import (
    InboundMessage, ContentType,
    SessionKey, UserIdentity, Platform, ChatType,
)

logger = logging.getLogger(__name__)

# Sentinel identity used for all heartbeat messages.
_HEARTBEAT_USER_ID = "heartbeat-system"
_HEARTBEAT_AUTHOR  = UserIdentity(
    platform=Platform.CLI,
    user_id=_HEARTBEAT_USER_ID,
    username="heartbeat",
)
_HEARTBEAT_SESSION = SessionKey(
    chat_type=ChatType.DM,
    conversation_id=_HEARTBEAT_USER_ID,
)
_TOKEN = "HEARTBEAT_OK"



def _resolve_session(session_cfg: str, main_key: "SessionKey") -> "SessionKey":
    """
    Resolve a session config value to a SessionKey.
      "main"              → the agent's own session key (default)
      "dm:<id>"           → SessionKey.dm(id)
      "group:<plat>:<id>" → SessionKey.group(Platform.<plat>, id)
    Any unrecognised string falls back to main.
    """
    if not session_cfg or session_cfg == "main":
        return main_key
    parts = session_cfg.split(":", 2)
    try:
        if parts[0] == "dm" and len(parts) == 2:
            return SessionKey.dm(parts[1])
        if parts[0] == "group" and len(parts) == 3:
            return SessionKey.group(Platform(parts[1]), parts[2])
    except Exception:
        pass
    logger.warning("[heartbeat] unrecognised session config '%s' — using main session", session_cfg)
    return main_key


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

    # Resolve session key. "main" (default) uses the agent's own session.
    # Any other string is parsed as "dm:<id>" or "group:<platform>:<id>".
    session_cfg = cfg.get("session", "main")
    session_key = _resolve_session(session_cfg, agent.session_key)

    interval_secs = every_minutes * 60

    task = asyncio.get_event_loop().create_task(
        _heartbeat_loop(
            agent, interval_secs,
            prompt, continuation_prompt,
            ack_max, max_continuations,
            active_hours, session_key,
        ),
        name=f"heartbeat:{agent.session_key}",
    )

    _patch_reset(agent, task)

    logger.info(
        "[heartbeat] started — every %dm, session=%s, active_hours=%s",
        every_minutes, session_key, active_hours,
    )


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def _heartbeat_loop(
    agent,
    interval_secs: int,
    prompt: str,
    continuation_prompt: str,
    ack_max: int,
    max_continuations: int,
    active_hours: dict | None,
    session_key: "SessionKey",
) -> None:
    # Wait one full interval before the first tick so startup isn't noisy.
    await asyncio.sleep(interval_secs)

    while True:
        try:
            if _in_active_window(active_hours):
                await _tick(
                    agent, prompt, continuation_prompt,
                    ack_max, max_continuations, session_key,
                )
            else:
                logger.debug("[heartbeat] outside active hours — skipping tick")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[heartbeat] unhandled error during tick")

        await asyncio.sleep(interval_secs)


# ---------------------------------------------------------------------------
# Single tick — initial turn + continuation loop
# ---------------------------------------------------------------------------

async def _tick(
    agent,
    prompt: str,
    continuation_prompt: str,
    ack_max: int,
    max_continuations: int,
    session_key: "SessionKey",
) -> None:
    """
    Run the initial heartbeat turn. If the agent doesn't reply HEARTBEAT_OK,
    re-prompt it to continue or acknowledge, up to max_continuations times.
    """
    logger.debug("[heartbeat] tick start")

    # Initial turn
    reply = await _run_turn(agent, prompt, session_key)
    is_ok, alert = _parse_reply(reply, ack_max)

    if is_ok:
        logger.debug("[heartbeat] OK on initial turn")
        return

    # Agent has work to do — enter continuation loop
    _emit_alert(alert)

    for turn in range(1, max_continuations + 1):
        logger.debug("[heartbeat] continuation turn %d/%d", turn, max_continuations)
        reply = await _run_turn(agent, continuation_prompt, session_key)
        is_ok, alert = _parse_reply(reply, ack_max)

        if is_ok:
            logger.debug("[heartbeat] OK after %d continuation turn(s)", turn)
            return

        _emit_alert(alert)

    logger.warning(
        "[heartbeat] max_continuations (%d) reached without HEARTBEAT_OK — giving up",
        max_continuations,
    )


async def _run_turn(agent, text: str, session_key: "SessionKey") -> str:
    """Inject a synthetic message and collect the full reply."""
    msg = InboundMessage(
        session_key=session_key,
        author=_HEARTBEAT_AUTHOR,
        content_type=ContentType.TEXT,
        text=text,
        message_id=f"heartbeat-{int(time.time_ns())}",
        timestamp=time.time(),
    )
    parts: list[str] = []
    async for chunk in agent.run(msg):
        parts.append(chunk.text)
        if not chunk.is_partial:
            break
    return "".join(parts).strip()


def _parse_reply(reply: str, ack_max: int) -> tuple[bool, str]:
    """
    Returns (is_ok, alert_text).

    Strips HEARTBEAT_OK from the start or end of the reply.
    is_ok=True when the remainder is ≤ ack_max chars (nothing meaningful to surface).
    is_ok=False when there's real content to deliver as an alert.
    """
    text = reply

    if text.startswith(_TOKEN):
        text = text[len(_TOKEN):].lstrip(" \n\r")
    elif text.endswith(_TOKEN):
        text = text[: -len(_TOKEN)].rstrip(" \n\r")

    is_ok = len(text) <= ack_max
    return is_ok, text


def _emit_alert(text: str) -> None:
    print(f"\n[HEARTBEAT ALERT]\n{text}\n")
    logger.info("[heartbeat] alert delivered (%d chars)", len(text))


# ---------------------------------------------------------------------------
# Active hours check
# ---------------------------------------------------------------------------

def _parse_hhmm(s: str) -> dtime:
    h, m = s.strip().split(":")
    return dtime(int(h), int(m))


def _in_active_window(active_hours: dict | None) -> bool:
    """Return True if we should run this tick."""
    if not active_hours:
        return True

    try:
        start = _parse_hhmm(active_hours["start"])
        end_  = _parse_hhmm(active_hours["end"])
    except (KeyError, ValueError):
        logger.warning("[heartbeat] invalid active_hours config — running anyway")
        return True

    # Zero-width window: always outside.
    if start == end_:
        return False

    now = datetime.now().time().replace(second=0, microsecond=0)

    # Handle midnight-spanning windows (e.g. 22:00 → 02:00)
    if start < end_:
        return start <= now < end_
    else:
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
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

Convention: register_agent(agent) — no imports from gateway or bridges.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path

from TinyCTX.contracts import (
    InboundMessage, ContentType, UserIdentity, Platform,
    AgentTextChunk, AgentTextFinal, AgentError
)

logger = logging.getLogger(__name__)

_HEARTBEAT_USER_ID = "heartbeat-system"
_HEARTBEAT_AUTHOR  = UserIdentity(
    platform=Platform.CRON,
    user_id=_HEARTBEAT_USER_ID,
    username="heartbeat",
)
_TOKEN = "HEARTBEAT_OK"


class _HeartbeatRunner:
    def __init__(self, runtime, cfg: dict) -> None:
        self.runtime = runtime
        self.cfg = cfg
        self.interval_secs = int(cfg.get("every_minutes", 30)) * 60
        self.cursor_node_id: str | None = None
        self._running = False

    def start(self):
        if self.interval_secs <= 0: return
        self._running = True
        asyncio.create_task(self._loop())

    async def _loop(self):
        # Initial delay to let the system stabilize
        await asyncio.sleep(10) 
        
        while self._running:
            if self._in_active_window():
                try:
                    await self._tick()
                except Exception:
                    logger.exception("[heartbeat] tick failed")
            
            await asyncio.sleep(self.interval_secs)

    async def _tick(self):
        # 1. Determine starting cursor
        if not self.cursor_node_id:
            self.cursor_node_id = self.runtime.db.get_root().id

        # 2. Continuation loop
        current_prompt = self.cfg.get("prompt", "Read HEARTBEAT.md if it exists. If nothing needs attention, reply HEARTBEAT_OK.")

        for _turn in range(int(self.cfg.get("max_continuations", 5))):
            msg = InboundMessage(
                tail_node_id=self.cursor_node_id or "",
                author=_HEARTBEAT_AUTHOR,
                content_type=ContentType.TEXT,
                text=current_prompt,
                message_id=f"heartbeat-{time.time()}",
                timestamp=time.time(),
                trigger=True,
            )

            reply_queue: asyncio.Queue = asyncio.Queue()
            new_node_id = await self.runtime.push(msg, reply_queue=reply_queue)

            full_text: list[str] = []
            final_tail: str | None = None
            timed_out = False

            try:
                while True:
                    try:
                        event = await asyncio.wait_for(reply_queue.get(), timeout=120)
                    except asyncio.TimeoutError:
                        logger.warning("[heartbeat] turn timed out")
                        timed_out = True
                        break

                    if event is None:  # sentinel
                        break

                    if isinstance(event, AgentTextFinal):
                        if event.text:
                            full_text.append(event.text)
                        final_tail = event.tail_node_id
                    elif isinstance(event, AgentTextChunk):
                        full_text.append(event.text)
                    elif isinstance(event, AgentError):
                        logger.error("[heartbeat] agent error: %s", event.message)
                        timed_out = True
                        break
            except Exception:
                logger.exception("[heartbeat] error draining reply queue")
                break

            if timed_out:
                break

            # Advance cursor to the real assistant tail
            if final_tail:
                self.cursor_node_id = final_tail
            else:
                self.cursor_node_id = new_node_id

            # Parse reply
            reply_content = "".join(full_text).strip()
            is_ok, alert = _parse_reply(reply_content, int(self.cfg.get("ack_max_chars", 300)))

            if is_ok:
                break

            logger.warning("[HEARTBEAT ALERT]\n%s", alert)
            current_prompt = self.cfg.get("continuation_prompt", "Continue the task, or reply HEARTBEAT_OK when done.")

    def _in_active_window(self) -> bool:
        hours = self.cfg.get("active_hours")
        if not hours: return True
        now = datetime.now().time()
        start = _parse_hhmm(hours["start"])
        end = _parse_hhmm(hours["end"])
        return start <= now <= end if start < end else now >= start or now <= end

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_global_runner: _HeartbeatRunner | None = None

def register_runtime(runtime) -> None:
    global _global_runner
    try:
        from TinyCTX.modules.heartbeat import EXTENSION_META
        cfg = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    # Guard: check for HEARTBEAT.md
    workspace = Path(runtime.config.workspace.path).expanduser().resolve()
    if not (workspace / "HEARTBEAT.md").exists():
        logger.info("[heartbeat] HEARTBEAT.md missing, disabled.")
        return

    _global_runner = _HeartbeatRunner(runtime, cfg)
    _global_runner.start()

    async def _cmd_run(args, context):
        asyncio.create_task(_global_runner._tick())
        send = context.get("send")
        if callable(send):
            await send("Heartbeat tick triggered manually.")

    runtime.commands.register("heartbeat", "run", _cmd_run, help="Manual heartbeat tick")


def register_agent(agent) -> None:
    pass


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

# ---------------------------------------------------------------------------
# Active hours
# ---------------------------------------------------------------------------

def _parse_hhmm(s: str) -> dtime:
    h, m = s.strip().split(":")
    return dtime(int(h), int(m))

"""
modules/cron/__main__.py

Scheduled agent turns backed by workspace/CRON.json.

Schedule kinds:
  every  — fixed interval (every_minutes or every_seconds)
  at     — one-shot UTC timestamp (at_ms); auto-disables after firing
  cron   — cron expression (expr + optional tz); requires `croniter`

Job schema (CRON.json):
  {
    "version": 1,
    "jobs": [
      {
        "id":             "abc12345",
        "name":           "daily-standup",
        "enabled":        true,
        "schedule": {
          "kind":         "every",          // "every" | "at" | "cron"
          "every_ms":     28800000,         // every: interval in ms
          "at_ms":        null,             // at: UTC epoch ms
          "expr":         null,             // cron: expression e.g. "0 9 * * *"
          "tz":           null              // cron: IANA timezone e.g. "America/New_York"
        },
        "message":        "Check calendar and summarise today's agenda.",
        "delete_after_run": false,
        "reset_after_run":  false,          // wipe this job's session context after each run
        "state": {
          "next_run_at_ms": 1234567890000,
          "last_run_at_ms": null,
          "last_status":    null,           // "ok" | "error" | "skipped"
          "last_error":     null
        },
        "created_at_ms":  1234567890000,
        "updated_at_ms":  1234567890000
      }
    ]
  }

The agent edits CRON.json directly using str_replace / view / create_file.
cron_list is the only tool registered — it validates and summarises jobs.

Convention: register(agent) — no imports from gateway or bridges.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import (
    InboundMessage, ContentType,
    UserIdentity, Platform,
)

logger = logging.getLogger(__name__)

_CRON_USER_ID = "cron-system"
_CRON_PLATFORM = Platform.CRON.value  # "cron" — used for platform handler slot

_CRON_AUTHOR = UserIdentity(
    platform=Platform.CRON,
    user_id=_CRON_USER_ID,
    username="cron",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CronSchedule:
    kind:     str        # "every" | "at" | "cron"
    every_ms: int | None = None
    at_ms:    int | None = None
    expr:     str | None = None
    tz:       str | None = None


@dataclass
class CronState:
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status:    str | None = None   # "ok" | "error" | "skipped"
    last_error:     str | None = None


@dataclass
class CronJob:
    id:               str
    name:             str
    enabled:          bool
    schedule:         CronSchedule
    message:          str
    state:            CronState  = field(default_factory=CronState)
    cursor_node_id:   str | None = None    # DB node_id for this job's branch cursor
    delete_after_run: bool       = False
    reset_after_run:  bool       = False   # wipe session context after each run
    created_at_ms:    int        = 0
    updated_at_ms:    int        = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _fmt_ts(ms: int | None) -> str:
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            # FIX (lint): alias import to avoid "croniter" shadowing itself —
            # `from croniter import croniter` makes the class name identical to
            # the module name, which confuses static analysers. Aliasing to
            # CronIter makes both references unambiguous.
            from croniter import croniter as CronIter
            from zoneinfo import ZoneInfo
            tz   = ZoneInfo(schedule.tz) if schedule.tz else timezone.utc
            base = datetime.fromtimestamp(now_ms / 1000, tz=tz)
            nxt  = CronIter(schedule.expr, base).get_next(datetime)
            return int(nxt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_job(job: CronJob, now_ms: int) -> list[str]:
    """Return a list of validation warnings for a job. Empty = valid."""
    warnings = []
    s = job.schedule

    if s.kind not in ("every", "at", "cron"):
        warnings.append(f"unknown schedule kind '{s.kind}'")
        return warnings

    if s.kind == "every":
        if not s.every_ms or s.every_ms <= 0:
            warnings.append("every_ms must be > 0")

    elif s.kind == "at":
        if s.at_ms is None:
            warnings.append("at_ms is required for 'at' schedules")
        elif s.at_ms <= now_ms and job.enabled:
            warnings.append("at_ms is in the past — job will never fire")
        if s.tz:
            warnings.append("tz has no effect on 'at' schedules")

    elif s.kind == "cron":
        if not s.expr:
            warnings.append("expr is required for 'cron' schedules")
        else:
            try:
                # FIX (lint): same alias here for consistency
                from croniter import croniter as CronIter
                if not CronIter.is_valid(s.expr):
                    warnings.append(f"invalid cron expression '{s.expr}'")
            except ImportError:
                warnings.append("croniter not installed — cron schedules disabled")
        if s.tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(s.tz)
            except Exception:
                warnings.append(f"unknown timezone '{s.tz}'")

    if not job.message.strip():
        warnings.append("message is empty — agent will receive no instructions")

    return warnings


# ---------------------------------------------------------------------------
# Store load / save
# ---------------------------------------------------------------------------

def _load_store(path: Path) -> tuple[list[CronJob], int]:
    """Load CRON.json. Returns (jobs, version)."""
    if not path.exists():
        return [], 1
    try:
        raw  = json.loads(path.read_text(encoding="utf-8"))
        jobs = []
        for j in raw.get("jobs", []):
            s = j.get("schedule", {})
            st = j.get("state", {})
            jobs.append(CronJob(
                id=j["id"],
                name=j["name"],
                enabled=j.get("enabled", True),
                schedule=CronSchedule(
                    kind=s.get("kind", "every"),
                    every_ms=s.get("every_ms"),
                    at_ms=s.get("at_ms"),
                    expr=s.get("expr"),
                    tz=s.get("tz"),
                ),
                message=j.get("message", ""),
                state=CronState(
                    next_run_at_ms=st.get("next_run_at_ms"),
                    last_run_at_ms=st.get("last_run_at_ms"),
                    last_status=st.get("last_status"),
                    last_error=st.get("last_error"),
                ),
                delete_after_run=j.get("delete_after_run", False),
                reset_after_run=j.get("reset_after_run", False),
                created_at_ms=j.get("created_at_ms", 0),
                updated_at_ms=j.get("updated_at_ms", 0),
            ))
        return jobs, int(raw.get("version", 1))
    except Exception as exc:
        logger.warning("[cron] failed to load CRON.json: %s", exc)
        return [], 1


def _save_store(path: Path, jobs: list[CronJob], version: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "version": version,
        "jobs": [
            {
                "id":    j.id,
                "name":  j.name,
                "enabled": j.enabled,
                "schedule": {
                    "kind":     j.schedule.kind,
                    "every_ms": j.schedule.every_ms,
                    "at_ms":    j.schedule.at_ms,
                    "expr":     j.schedule.expr,
                    "tz":       j.schedule.tz,
                },
                "message": j.message,
                "delete_after_run": j.delete_after_run,
                "reset_after_run":  j.reset_after_run,
                "state": {
                    "next_run_at_ms": j.state.next_run_at_ms,
                    "last_run_at_ms": j.state.last_run_at_ms,
                    "last_status":    j.state.last_status,
                    "last_error":     j.state.last_error,
                },
                "created_at_ms": j.created_at_ms,
                "updated_at_ms": j.updated_at_ms,
            }
            for j in jobs
        ],
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# cron_list tool
# ---------------------------------------------------------------------------

def _build_cron_list(path: Path) -> str:
    now = _now_ms()

    if not path.exists():
        return (
            "No CRON.json found in workspace.\n\n"
            f"To create scheduled jobs, create {path} using create_file.\n"
            "See the CRON.json schema in the cron module docstring for the format."
        )

    try:
        jobs, _ = _load_store(path)
    except Exception as exc:
        return f"⚠ Could not parse CRON.json: {exc}\n\nFix the file using str_replace."

    if not jobs:
        return (
            "CRON.json exists but contains no jobs.\n\n"
            "To add a job, edit workspace/CRON.json using str_replace or view."
        )

    lines = [f"{len(jobs)} cron job{'s' if len(jobs) != 1 else ''}:\n"]

    for j in jobs:
        warnings = _validate_job(j, now)
        status_icon = "✓" if j.enabled else "–"
        if warnings:
            status_icon = "⚠"

        s = j.schedule
        if s.kind == "every" and s.every_ms:
            mins = s.every_ms // 60000
            hrs  = mins // 60
            if hrs and not mins % 60:
                sched_str = f"every {hrs}h"
            elif hrs:
                sched_str = f"every {hrs}h {mins % 60}m"
            else:
                sched_str = f"every {mins}m"
        elif s.kind == "at":
            sched_str = f"at {_fmt_ts(s.at_ms)}"
        elif s.kind == "cron":
            sched_str = f'cron "{s.expr}"'
            if s.tz:
                sched_str += f" ({s.tz})"
        else:
            sched_str = f"unknown kind '{s.kind}'"

        disabled_str = " [disabled]" if not j.enabled else ""
        lines.append(f"[{j.id}] {j.name} — {sched_str}{disabled_str} {status_icon}")

        lines.append(f"  next: {_fmt_ts(j.state.next_run_at_ms)}  |  last: ")
        if j.state.last_status:
            last = j.state.last_status
            if j.state.last_error:
                last += f" — \"{j.state.last_error}\""
            lines[-1] += f"{last} ({_fmt_ts(j.state.last_run_at_ms)})"
        else:
            lines[-1] += "never run"

        preview = j.message if len(j.message) <= 60 else j.message[:57] + "..."
        lines.append(f"  msg: {preview}")

        for w in warnings:
            lines.append(f"  ⚠ {w}")

        lines.append("")

    lines.append(
        "To add, edit, or remove jobs, edit workspace/CRON.json directly using str_replace or view."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cron runner
# ---------------------------------------------------------------------------

class _CronRunner:
    """
    Watches CRON.json for changes and fires due jobs.
    Each job is pushed through the gateway so it gets its own isolated Lane
    and AgentLoop — job context never bleeds into the user's session.

    Concurrency model: jobs within a single tick run sequentially under
    _job_lock, so the shared _CRON_PLATFORM reply-handler slot is never
    contested between jobs. Ticks themselves are serialised because _arm()
    only schedules the next tick after _on_timer() completes.
    """

    def __init__(self, agent, store_path: Path) -> None:
        self._agent = agent
        self._store_path = store_path
        self._jobs:       list[CronJob] = []
        self._version:    int           = 1
        self._last_mtime: float         = 0.0
        self._timer_task: asyncio.Task | None = None
        self._running     = False
        # Serialises job execution so the cron reply-handler slot is never
        # shared between two concurrently-running jobs.
        self._job_lock    = asyncio.Lock()

    def start(self) -> None:
        self._running = True
        self._reload_if_changed()
        self._recompute_next_runs()
        self._save()
        self._ensure_noop_platform_handler()
        self._arm()
        logger.info("[cron] started with %d job(s)", len(self._jobs))

    def stop(self) -> None:
        self._running = False
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

    # ------------------------------------------------------------------

    def _reload_if_changed(self) -> None:
        if self._store_path.exists():
            mtime = self._store_path.stat().st_mtime
            if mtime != self._last_mtime:
                self._jobs, self._version = _load_store(self._store_path)
                self._last_mtime = mtime
                logger.debug("[cron] reloaded CRON.json (%d jobs)", len(self._jobs))
        else:
            self._jobs    = []
            self._version = 1

    def _recompute_next_runs(self) -> None:
        now = _now_ms()
        for j in self._jobs:
            if j.enabled:
                j.state.next_run_at_ms = _compute_next_run(j.schedule, now)

    def _save(self) -> None:
        if self._jobs:
            _save_store(self._store_path, self._jobs, self._version)

    def _next_wake_ms(self) -> int | None:
        times = [
            j.state.next_run_at_ms
            for j in self._jobs
            if j.enabled and j.state.next_run_at_ms is not None
        ]
        return min(times) if times else None

    def _arm(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        wake = self._next_wake_ms()
        if wake is None or not self._running:
            return

        delay_s = max(0.0, (wake - _now_ms()) / 1000)

        async def _tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.get_event_loop().create_task(_tick())

    def _ensure_noop_platform_handler(self) -> None:
        gateway = getattr(self._agent, "gateway", None)
        if gateway is None:
            return

        handlers = getattr(gateway, "_platform_handlers", None)
        if not isinstance(handlers, dict):
            return

        if handlers.get(_CRON_PLATFORM) is None:
            handlers[_CRON_PLATFORM] = _noop_reply_handler
            register = getattr(gateway, "register_platform_handler", None)
            if callable(register):
                try:
                    register(_CRON_PLATFORM, _noop_reply_handler)
                except Exception:
                    pass

    async def _on_timer(self) -> None:
        self._reload_if_changed()
        now = _now_ms()

        due = [
            j for j in self._jobs
            if j.enabled
            and j.state.next_run_at_ms is not None
            and now >= j.state.next_run_at_ms
        ]

        for job in due:
            async with self._job_lock:
                await self._run_job(job)

        self._save()
        self._arm()

    def _get_or_create_job_cursor(self, job: CronJob, gateway) -> str:
        """
        Return the node_id for this job's branch cursor.
        Creates a new child of the global root on first call and stores it
        in job.cursor_node_id so subsequent runs resume where the last left off.
        """
        if job.cursor_node_id:
            return job.cursor_node_id
        from db import ConversationDB
        workspace = Path(self._agent.config.workspace.path).expanduser().resolve()
        db   = ConversationDB(workspace / "agent.db")
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="system", content=f"session:cron:{job.id}")
        job.cursor_node_id = node.id
        logger.info("[cron] created cursor node %s for job '%s'", node.id, job.name)
        return node.id

    async def _run_job(self, job: CronJob) -> None:
        logger.info("[cron] running job '%s' (%s)", job.name, job.id)
        start_ms = _now_ms()

        try:
            gateway = getattr(self._agent, "gateway", None)
            if gateway is None:
                logger.error("[cron] agent.gateway not set — cannot run job '%s'", job.name)
                job.state.last_status = "error"
                job.state.last_error  = "agent.gateway not available"
                return

            # Normal app startup sets agent.gateway after module registration,
            # so keep a lazy path here even though start() also registers when
            # the gateway is already available.
            self._ensure_noop_platform_handler()

            node_id = self._get_or_create_job_cursor(job, gateway)

            msg = InboundMessage(
                tail_node_id=node_id,
                author=_CRON_AUTHOR,
                content_type=ContentType.TEXT,
                text=job.message,
                message_id=f"cron-{job.id}-{int(time.time_ns())}",
                timestamp=time.time(),
            )

            parts: list[str] = []
            reply_event = asyncio.Event()

            async def _collect(event) -> None:
                from contracts import AgentTextChunk, AgentTextFinal, AgentError
                if isinstance(event, AgentTextChunk):
                    parts.append(event.text)
                elif isinstance(event, AgentTextFinal):
                    if event.text:
                        parts.append(event.text)
                    reply_event.set()
                elif isinstance(event, AgentError):
                    parts.append(event.message)
                    reply_event.set()

            # Use per-cursor handler so concurrent jobs don't share a slot.
            gateway.register_cursor_handler(node_id, _collect)

            try:
                await gateway.push(msg)
                await asyncio.wait_for(reply_event.wait(), timeout=120)
            except asyncio.TimeoutError:
                # Abort the lane before unregistering the cursor handler so the
                # AgentLoop stops emitting events.  Re-raise so the outer
                # except block records the timeout status.
                gateway.abort_generation(node_id)
                raise
            finally:
                gateway.unregister_cursor_handler(node_id)

            reply_text = "".join(parts).strip()
            if reply_text:
                logger.info("[cron] job '%s' reply:\n%s", job.name, reply_text)

            # Advance the job cursor to the lane's latest tail so the next run
            # continues from the updated branch head.
            lane_router = getattr(gateway, "_lane_router", None)
            lanes = getattr(lane_router, "_lanes", None)
            if isinstance(lanes, dict):
                lane = lanes.get(node_id)
                loop_owner = lane
                if getattr(loop_owner, "loop", None) is None:
                    nested_lane = getattr(loop_owner, "_lane", None)
                    if nested_lane is not None:
                        loop_owner = nested_lane
                loop = getattr(loop_owner, "loop", None)
                tail = getattr(loop, "_tail_node_id", None)
                if tail:
                    job.cursor_node_id = tail

            job.state.last_status = "ok"
            job.state.last_error  = None
            logger.info("[cron] job '%s' completed", job.name)

        except asyncio.TimeoutError:
            job.state.last_status = "error"
            job.state.last_error  = "timed out after 120s"
            logger.error("[cron] job '%s' timed out", job.name)
        except Exception as exc:
            job.state.last_status = "error"
            job.state.last_error  = str(exc)
            logger.error("[cron] job '%s' failed: %s", job.name, exc)

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms        = _now_ms()

        # reset_after_run: clear in-memory context (tree stays intact).
        if job.reset_after_run:
            gateway = getattr(self._agent, "gateway", None)
            if gateway is not None and job.cursor_node_id:
                gateway.reset_lane(job.cursor_node_id)
                logger.debug("[cron] reset lane for job '%s'", job.name)

        # One-shot cleanup
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._jobs = [j for j in self._jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())


# ---------------------------------------------------------------------------
# Module-level no-op handler — installed as the cron platform sentinel
# so the slot is never empty between job runs.
# ---------------------------------------------------------------------------

async def _noop_reply_handler(reply) -> None:
    pass


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def register(agent) -> None:
    try:
        from modules.cron import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    workspace  = Path(agent.config.workspace.path).expanduser().resolve()
    store_path = workspace / cfg.get("store_file", "CRON.json")

    runner = _CronRunner(agent, store_path)
    runner.start()

    original_reset = agent.reset
    def patched_reset():
        original_reset()
        runner.stop()
        logger.info("[cron] runner stopped on session reset")
    agent.reset = patched_reset

    def cron_list() -> str:
        """
        List all scheduled cron jobs, validate their configuration, and show next/last run times.
        To add, edit, or remove jobs, edit workspace/CRON.json directly using str_replace or view.
        """
        return _build_cron_list(store_path)

    agent.tool_handler.register_tool(cron_list)

    logger.info("[cron] registered — store: %s", store_path)

"""
modules/cron/__main__.py

Scheduled agent turns backed by a SQLite store at config.data.path/cron.db.

v2 rewrite — three problems v1 (workspace/CRON.json, agent-editable, one
synthetic runner identity) had:

  1. No out-pipe: v1 discarded every AgentEvent a job's turn produced. Job
     output is now delivered back to the job's origin channel via
     Runtime.deliver(platform, cursor_key, event) — the same per-platform
     renderer a live user turn uses (see bridges/discord/turn.py,
     bridges/telegram/__main__.py; wired up in runtime.py).

  2. Hardcoded permission: v1 ran every job as one synthetic "cron-system"
     user, so permission-gated tools were either open to all cron jobs or
     none. Jobs now store the *real* creating user's username, and at run
     time the job executes with that user's *current* permission_level
     (re-resolved via UserStore.get_user, not cached) — a promotion or
     demotion since the job was created takes effect on the job's very
     next run.

  3. Indirect prompt injection via file write: v1 stored jobs as plain JSON
     in workspace/, which the agent's own filesystem tools (edit_file,
     write_file) can read and write — so any untrusted content the agent
     was asked to "save" could plant or rewrite a job's message, later
     executed at cron's permission level. Jobs are now rows in a SQLite
     database under config.data.path (same directory as agent.db /
     users.db), which the agent's filesystem tools never see, and the only
     way to create/inspect/remove a job is through the add_cron / list_cron
     / remove_cron tools below — there is no file for the agent to edit.

Schedule kinds (unchanged from v1):
  every  — fixed interval (every_ms)
  at     — one-shot UTC timestamp (at_ms); auto-disables after firing
  cron   — cron expression (expr + optional tz); requires `croniter`

Channel isolation: every job stores the cursor_key of the channel it was
created in (the same stable "group:<id>" / "tg:<id>" address bridges use
for cursor persistence — see bridges/discord/cursors.py). Runtime.push()
now records the caller's session_key onto each node's state_delta as
"cursor_key" (runtime.py: Runtime._compute_state_delta), which is how
add_cron recovers "what channel is this turn happening in" without
importing any bridge module. list_cron only returns jobs whose cursor_key
matches the caller's current channel; remove_cron only acts on a job in
the caller's channel, and additionally requires either being the job's
creator or having permission_level >= admin_override_permission (config,
default 90) — so Alice cannot see or delete Bob's job in Bob's channel, and
a same-channel non-admin cannot delete another user's job either.

Convention: register_agent(agent) — no imports from gateway or bridges
(delivery goes through Runtime.deliver, which is bridge-agnostic).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from TinyCTX.contracts import (
    AgentError, AgentTextFinal,
    InboundMessage, SessionEnvironment, ContentType,
    Platform,
)

logger = logging.getLogger(__name__)


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
    creator_username: str          # real TinyCTX username that created this job
    platform:         str          # Platform.value the job's channel lives on
    cursor_key:       str          # channel/chat identity — see cursors.py
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
            # Aliased import — see v1 note: `croniter` as both module and
            # class name confuses static analysers if not aliased.
            from croniter import croniter as CronIter
            from zoneinfo import ZoneInfo
            tz   = ZoneInfo(schedule.tz) if schedule.tz else timezone.utc
            base = datetime.fromtimestamp(now_ms / 1000, tz=tz)
            nxt  = CronIter(schedule.expr, base).get_next(datetime)
            return int(nxt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule(kind: str, every_ms: int | None, at_ms: int | None,
                        expr: str | None, tz: str | None, now_ms: int) -> str | None:
    """Return an error string if the requested schedule is invalid, else None."""
    if kind not in ("every", "at", "cron"):
        return f"unknown schedule kind {kind!r} — must be 'every', 'at', or 'cron'"

    if kind == "every":
        if not every_ms or every_ms <= 0:
            return "every_ms must be > 0 for kind='every'"

    elif kind == "at":
        if at_ms is None:
            return "at_ms is required for kind='at'"
        if at_ms <= now_ms:
            return "at_ms is in the past — job would never fire"

    elif kind == "cron":
        if not expr:
            return "expr is required for kind='cron'"
        try:
            from croniter import croniter as CronIter
            if not CronIter.is_valid(expr):
                return f"invalid cron expression {expr!r}"
        except ImportError:
            return "croniter not installed — cron-kind schedules are unavailable"
        if tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:
                return f"unknown timezone {tz!r}"

    return None


# ---------------------------------------------------------------------------
# CronStore — SQLite, under config.data.path (never workspace/).
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS cron_jobs (
    id                TEXT PRIMARY KEY,
    creator_username  TEXT NOT NULL,
    platform          TEXT NOT NULL,
    cursor_key        TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    schedule_kind     TEXT NOT NULL,
    schedule_every_ms INTEGER,
    schedule_at_ms    INTEGER,
    schedule_expr     TEXT,
    schedule_tz       TEXT,
    message           TEXT NOT NULL,
    delete_after_run  INTEGER NOT NULL DEFAULT 0,
    reset_after_run   INTEGER NOT NULL DEFAULT 0,
    cursor_node_id    TEXT,
    next_run_at_ms    INTEGER,
    last_run_at_ms    INTEGER,
    last_status       TEXT,
    last_error        TEXT,
    created_at_ms     INTEGER NOT NULL,
    updated_at_ms     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cron_jobs_cursor_key ON cron_jobs(cursor_key);
"""


def _row_to_job(row: sqlite3.Row) -> CronJob:
    return CronJob(
        id=row["id"],
        creator_username=row["creator_username"],
        platform=row["platform"],
        cursor_key=row["cursor_key"],
        enabled=bool(row["enabled"]),
        schedule=CronSchedule(
            kind=row["schedule_kind"],
            every_ms=row["schedule_every_ms"],
            at_ms=row["schedule_at_ms"],
            expr=row["schedule_expr"],
            tz=row["schedule_tz"],
        ),
        message=row["message"],
        state=CronState(
            next_run_at_ms=row["next_run_at_ms"],
            last_run_at_ms=row["last_run_at_ms"],
            last_status=row["last_status"],
            last_error=row["last_error"],
        ),
        cursor_node_id=row["cursor_node_id"],
        delete_after_run=bool(row["delete_after_run"]),
        reset_after_run=bool(row["reset_after_run"]),
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


class CronStore:
    """
    SQLite-backed cron job store. Lives under config.data.path — the
    instance's internal data dir, alongside agent.db and users.db — which
    the agent's own filesystem tools never see (see runtime.py's docstring
    distinguishing workspace/ from data_path). This is what makes "the
    agent edits CRON.json directly" (v1's whole interaction model, and its
    prompt-injection surface) structurally impossible in v2: there is no
    file here for a tool call to reach.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()
        logger.info("[cron] store at %s", db_path)

    def add(self, job: CronJob) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO cron_jobs (
                    id, creator_username, platform, cursor_key, enabled,
                    schedule_kind, schedule_every_ms, schedule_at_ms, schedule_expr, schedule_tz,
                    message, delete_after_run, reset_after_run, cursor_node_id,
                    next_run_at_ms, last_run_at_ms, last_status, last_error,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id, job.creator_username, job.platform, job.cursor_key, int(job.enabled),
                    job.schedule.kind, job.schedule.every_ms, job.schedule.at_ms,
                    job.schedule.expr, job.schedule.tz,
                    job.message, int(job.delete_after_run), int(job.reset_after_run), job.cursor_node_id,
                    job.state.next_run_at_ms, job.state.last_run_at_ms,
                    job.state.last_status, job.state.last_error,
                    job.created_at_ms, job.updated_at_ms,
                ),
            )

    def get(self, job_id: str) -> CronJob | None:
        row = self._conn.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def all(self) -> list[CronJob]:
        rows = self._conn.execute("SELECT * FROM cron_jobs ORDER BY created_at_ms").fetchall()
        return [_row_to_job(r) for r in rows]

    def by_cursor_key(self, cursor_key: str) -> list[CronJob]:
        rows = self._conn.execute(
            "SELECT * FROM cron_jobs WHERE cursor_key = ? ORDER BY created_at_ms", (cursor_key,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def delete(self, job_id: str) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0

    def save_state(self, job: CronJob) -> None:
        """Persist mutable per-run fields only (state + cursor_node_id + enabled)."""
        with self._conn:
            self._conn.execute(
                """UPDATE cron_jobs SET
                    enabled = ?, cursor_node_id = ?,
                    next_run_at_ms = ?, last_run_at_ms = ?, last_status = ?, last_error = ?,
                    updated_at_ms = ?
                WHERE id = ?""",
                (
                    int(job.enabled), job.cursor_node_id,
                    job.state.next_run_at_ms, job.state.last_run_at_ms,
                    job.state.last_status, job.state.last_error,
                    _now_ms(), job.id,
                ),
            )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# list_cron formatting
# ---------------------------------------------------------------------------

def _format_job_line(j: CronJob) -> list[str]:
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

    status_icon = "✓" if j.enabled else "–"
    disabled_str = " [disabled]" if not j.enabled else ""
    lines = [f"[{j.id}] by {j.creator_username} — {sched_str}{disabled_str} {status_icon}"]

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
    return lines


def _build_cron_list(store: CronStore, cursor_key: str) -> str:
    jobs = store.by_cursor_key(cursor_key)
    if not jobs:
        return "No cron jobs in this channel. Use add_cron to create one."

    lines = [f"{len(jobs)} cron job{'s' if len(jobs) != 1 else ''} in this channel:\n"]
    for j in jobs:
        lines.extend(_format_job_line(j))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _CronRunner
# ---------------------------------------------------------------------------

class _CronRunner:
    """
    Watches CronStore and triggers turns via the Runtime, delivering each
    job's output back to its origin channel via Runtime.deliver().
    """
    def __init__(self, runtime, store: CronStore, min_run_permission: int) -> None:
        self.runtime = runtime
        self._store = store
        self._min_run_permission = min_run_permission
        self._jobs: list[CronJob] = []
        self._timer_task: asyncio.Task | None = None
        self._running = False
        self._job_lock = asyncio.Lock()

    def start(self) -> None:
        self._running = True
        self._reload()
        self._recompute_next_runs()
        self._arm()
        logger.info("[cron] runner started")

    def _reload(self) -> None:
        self._jobs = self._store.all()

    def _recompute_next_runs(self) -> None:
        now = _now_ms()
        for j in self._jobs:
            if j.enabled and j.state.next_run_at_ms is None:
                j.state.next_run_at_ms = _compute_next_run(j.schedule, now)
                self._store.save_state(j)

    def _next_wake_ms(self) -> int | None:
        enabled_jobs = [
            j.state.next_run_at_ms
            for j in self._jobs
            if j.enabled and j.state.next_run_at_ms is not None
        ]
        return min(enabled_jobs) if enabled_jobs else None

    def _arm(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        wake = self._next_wake_ms()
        if wake is None or not self._running:
            return

        delay = max(0, (wake - _now_ms()) / 1000)
        self._timer_task = asyncio.create_task(self._tick(delay))

    async def _tick(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if not self._running:
                return

            self._reload()
            now = _now_ms()
            due = [j for j in self._jobs if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms]

            for job in due:
                async with self._job_lock:
                    try:
                        await self._run_job(job)
                    except Exception:
                        # A single job's unexpected failure must never take
                        # down the tick. v1's _tick had no such guard, so an
                        # exception here (or in _reload / the due list
                        # comprehension) killed the create_task coroutine
                        # outright: no future tick was ever armed again, and
                        # nothing logged that the scheduler had silently
                        # died. Catch broadly so at worst one job's run is
                        # skipped, never the scheduler itself.
                        logger.exception("[cron] job '%s' run raised unexpectedly", job.id)
                        job.state.last_status = "error"
                        job.state.last_error = "internal error — see server logs"
                        job.state.last_run_at_ms = now
                        if job.schedule.kind == "at":
                            job.enabled = False
                        else:
                            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                        await asyncio.to_thread(self._store.save_state, job)
        except Exception:
            logger.exception("[cron] tick failed outside job execution — scheduler will still re-arm")
        finally:
            # Always re-arm, even if something above raised — this is the
            # single most important line in the runner: v1 had no `finally`
            # here, so any unhandled exception in this coroutine permanently
            # stopped the scheduler with no error surfaced anywhere.
            self._arm()

    async def _run_job(self, job: CronJob) -> None:
        start_ms = _now_ms()

        # 1. Re-resolve the job's *current* permission level from the real
        # creator's user record — not a cached/synthetic value. A demotion
        # since the job was created takes effect on this run.
        creator = self.runtime.users.get_user(job.creator_username)
        if creator is None:
            job.state.last_status = "error"
            job.state.last_error = f"creator user {job.creator_username!r} no longer exists"
            job.state.last_run_at_ms = start_ms
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
            await asyncio.to_thread(self._store.save_state, job)
            logger.warning("[cron] job '%s' skipped — creator user missing", job.id)
            return

        if creator.permission_level < self._min_run_permission:
            job.state.last_status = "skipped"
            job.state.last_error = (
                f"creator '{creator.username}' permission_level "
                f"{creator.permission_level} < required {self._min_run_permission}"
            )
            job.state.last_run_at_ms = start_ms
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
            await asyncio.to_thread(self._store.save_state, job)
            logger.info("[cron] job '%s' skipped — creator permission too low", job.id)
            return

        logger.info("[cron] running job '%s' (creator=%s, reset=%s)",
                     job.id, job.creator_username, job.reset_after_run)

        # 2. Determine the starting cursor, same staleness guard as v1.
        if job.reset_after_run or not job.cursor_node_id or not self.runtime.db.get_node(job.cursor_node_id):
            if job.cursor_node_id and not job.reset_after_run:
                logger.warning(
                    "[cron] job '%s' cursor %s no longer exists — resetting to root",
                    job.id, job.cursor_node_id,
                )
            parent_id = self.runtime.db.get_root().id
        else:
            parent_id = job.cursor_node_id

        # 3. Prepare the turn — runs as the real creator, so permission-gated
        # tools see this job's actual current permission_level, not a fixed
        # system identity's.
        msg = InboundMessage(
            tail_node_id=parent_id,
            author=creator,
            env=SessionEnvironment(platform=Platform.CRON),
            content_type=ContentType.TEXT,
            text=job.message,
            message_id=str(start_ms),
            timestamp=start_ms / 1000,
            trigger=True,
        )

        reply_queue: asyncio.Queue = asyncio.Queue()

        try:
            # 4. Push — returns the user node id; events stream into reply_queue.
            # No session_key passed: a cron run is its own one-off internal
            # session (see push()'s docstring), invisible to the concurrent-
            # forks roster — matches v1's behavior exactly.
            await self.runtime.push(msg, reply_queue=reply_queue)

            # 5. Drain the queue, delivering each event to the job's origin
            # channel via the platform's registered renderer (see
            # Runtime.register_platform_handler / deliver), and track the
            # final assistant tail to advance the cursor.
            final_tail: str | None = None
            while True:
                try:
                    event = await asyncio.wait_for(reply_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning("[cron] job '%s' timed out", job.id)
                    break

                if event is None:  # sentinel — turn complete
                    break

                await self.runtime.deliver(job.platform, job.cursor_key, event)

                if isinstance(event, AgentTextFinal) and event.tail_node_id:
                    final_tail = event.tail_node_id
                elif isinstance(event, AgentError):
                    raise RuntimeError(event.message)

            if final_tail:
                job.cursor_node_id = final_tail

            job.state.last_status = "ok"
            job.state.last_error = None

        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error("[cron] job '%s' failed: %s", job.id, e)

        # 6. Housekeeping.
        job.state.last_run_at_ms = start_ms
        if job.schedule.kind == "at":
            job.enabled = False
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

        if job.delete_after_run and job.schedule.kind != "every":
            await asyncio.to_thread(self._store.delete, job.id)
        else:
            await asyncio.to_thread(self._store.save_state, job)


# ---------------------------------------------------------------------------
# register_runtime() / register_agent()
# ---------------------------------------------------------------------------

_STORE_CACHE: CronStore | None = None


def _get_config() -> dict:
    from TinyCTX.modules.cron import EXTENSION_META
    return EXTENSION_META.get("default_config", {})


def register_runtime(runtime) -> None:
    """
    Called once at boot. Opens the CronStore under config.data.path and
    starts the background runner that triggers due jobs.
    """
    global _STORE_CACHE

    cfg = _get_config()
    data_path = Path(runtime.config.data.path).expanduser().resolve()
    store_file = cfg.get("store_file", "cron.db")
    store = CronStore(data_path / store_file)
    _STORE_CACHE = store

    min_run_permission = int(cfg.get("min_run_permission", 0))
    runner = _CronRunner(runtime, store, min_run_permission)
    runner.start()

    logger.info("[cron] background runner active via register_runtime")


def register_agent(agent) -> None:
    """
    Called per-turn. Injects add_cron / list_cron / remove_cron, scoped to
    the calling turn's real user identity and channel (cursor_key).
    """
    store = _STORE_CACHE
    if store is None:
        logger.error("[cron] register_agent called before register_runtime — cron tools unavailable this turn")
        return

    cfg = _get_config()
    min_create_permission = int(cfg.get("min_create_permission", 25))
    admin_override_permission = int(cfg.get("admin_override_permission", 90))

    def _current_channel() -> tuple[str, str] | None:
        """
        Recover (platform, cursor_key) for the channel this turn is running
        in, from the current tail node's session state. Runtime._compute_state_delta
        writes both onto every node's state_delta on every push() from a
        real bridge call (session_key IS the bridge's cursor_key — see
        runtime.py). Internal/one-off pushes (cron's own runs, in
        particular) never set session_key, so their nodes carry no
        cursor_key — which is exactly why add_cron called from *within* a
        cron-triggered turn correctly fails closed below rather than
        silently adopting the firing job's own address.
        """
        tail = agent.context.tail_node_id if agent.context else None
        if not tail or agent.db is None:
            return None
        cursor_key = agent.db.get_state(tail, "cursor_key", None)
        platform = agent.db.get_state(tail, "platform", None)
        if not cursor_key or not platform:
            return None
        return platform, cursor_key

    def add_cron(schedule_kind: str, message: str, every_ms: int = 0,
                 at_ms: int = 0, expr: str = "", tz: str = "",
                 delete_after_run: bool = False, reset_after_run: bool = False) -> str:
        """
        Create a new scheduled job in this channel. The job later runs as
        you (this call's caller) — with whatever permission_level you hold
        at run time, re-checked on every fire, not frozen at creation.

        Args:
            schedule_kind: "every" (fixed interval), "at" (one-shot timestamp), or "cron" (cron expression).
            message: The instruction the agent receives when this job fires.
            every_ms: Interval in milliseconds — required for schedule_kind="every".
            at_ms: UTC epoch milliseconds — required for schedule_kind="at".
            expr: Cron expression e.g. "0 9 * * *" — required for schedule_kind="cron".
            tz: IANA timezone e.g. "America/New_York" — optional, only used for schedule_kind="cron".
            delete_after_run: If true, delete this job after it fires once (ignored for "every").
            reset_after_run: If true, wipe this job's own session context after each run.
        """
        caller = agent.caller
        if caller is None:
            return json.dumps({"status": "error", "error": "Cannot resolve your identity."})
        if caller.permission_level < min_create_permission:
            return json.dumps({
                "status": "error",
                "error": f"permission_level {caller.permission_level} < required {min_create_permission} to create cron jobs.",
            })

        channel = _current_channel()
        if channel is None:
            return json.dumps({
                "status": "error",
                "error": "Could not determine this channel's identity — cron jobs cannot be created here.",
            })
        platform, cursor_key = channel

        message = (message or "").strip()
        if not message:
            return json.dumps({"status": "error", "error": "message must not be empty."})

        now = _now_ms()
        err = _validate_schedule(
            schedule_kind, every_ms or None, at_ms or None, expr or None, tz or None, now,
        )
        if err:
            return json.dumps({"status": "error", "error": err})

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            creator_username=caller.username,
            platform=platform,
            cursor_key=cursor_key,
            enabled=True,
            schedule=CronSchedule(
                kind=schedule_kind,
                every_ms=every_ms or None,
                at_ms=at_ms or None,
                expr=expr or None,
                tz=tz or None,
            ),
            message=message,
            delete_after_run=bool(delete_after_run),
            reset_after_run=bool(reset_after_run),
            created_at_ms=now,
            updated_at_ms=now,
        )
        job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
        store.add(job)
        logger.info("[cron] job '%s' created by %s in %s", job.id, caller.username, cursor_key)
        return json.dumps({"status": "ok", "job_id": job.id, "next_run": _fmt_ts(job.state.next_run_at_ms)})

    def list_cron() -> str:
        """
        List scheduled cron jobs in this channel only — jobs created in a
        different channel are never shown here, even to an admin.
        """
        channel = _current_channel()
        if channel is None:
            return "Could not determine this channel's identity."
        _, cursor_key = channel
        return _build_cron_list(store, cursor_key)

    def remove_cron(job_id: str) -> str:
        """
        Remove a scheduled job by id (see list_cron). You must either be
        the job's creator, or hold a high enough permission_level to
        override in your own channel — either way, only within this
        channel; you cannot remove a job from a different channel.

        Args:
            job_id: The job id shown by list_cron.
        """
        caller = agent.caller
        if caller is None:
            return json.dumps({"status": "error", "error": "Cannot resolve your identity."})

        channel = _current_channel()
        if channel is None:
            return json.dumps({"status": "error", "error": "Could not determine this channel's identity."})
        _, cursor_key = channel

        job = store.get(job_id)
        if job is None:
            return json.dumps({"status": "error", "error": f"No such job '{job_id}'."})

        if job.cursor_key != cursor_key:
            # Deliberately identical error to "not found" — do not reveal
            # that a job with this id exists in a different channel.
            return json.dumps({"status": "error", "error": f"No such job '{job_id}'."})

        is_creator = job.creator_username == caller.username
        is_admin = caller.permission_level >= admin_override_permission
        if not (is_creator or is_admin):
            return json.dumps({
                "status": "error",
                "error": "Only the job's creator, or a channel admin, may remove it.",
            })

        store.delete(job_id)
        logger.info("[cron] job '%s' removed by %s (creator=%s)", job_id, caller.username, job.creator_username)
        return json.dumps({"status": "ok"})

    agent.tool_handler.register_tool(add_cron, min_permission=min_create_permission)
    agent.tool_handler.register_tool(list_cron, always_on=True, min_permission=0)
    agent.tool_handler.register_tool(remove_cron, min_permission=0)

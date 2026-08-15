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
     next run. The agent-facing tools never mention this: the agent isn't
     "running the job as itself" in any permission sense — see add_cron's
     docstring, which is written entirely from the agent's point of view
     (schedule things, reminders/recurring checks, message goes to itself).

  3. Indirect prompt injection via file write: v1 stored jobs as plain JSON
     in workspace/, which the agent's own filesystem tools (edit_file,
     write_file) can read and write — so any untrusted content the agent
     was asked to "save" could plant or rewrite a job's message, later
     executed at cron's permission level. Jobs are now rows in a SQLite
     database under config.data.path (same directory as agent.db /
     users.db), which the agent's filesystem tools never see, and the only
     way to create/inspect/remove a job is through the add_cron / list_cron
     / remove_cron tools below — there is no file for the agent to edit.

Schedule surface (v2 simplification — v1 had three schedule kinds: "every"
fixed-interval, "at" one-shot timestamp, and "cron" expression, each with
its own set of args). A cron expression alone covers everything calendar-
shaped ("every day at 9am", "every 30 minutes" via */30, "every Monday") —
the only thing it can't express is a relative one-shot delay ("in 20
minutes") without computing an absolute future timestamp first. So v2 keeps
a single schedule param, `cron_expr`, plus `one_shot: bool` (replaces v1's
separate "at" kind — the job auto-disables after firing once, same as v1's
`at` behavior, but expressed as a flag on a cron expression rather than a
different schedule shape). For "in 20 minutes"-style asks, add_cron's
docstring tells the agent to compute a one-shot expression from the current
time it already has in context, and set one_shot=True.

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
    expr:     str               # cron expression, e.g. "0 9 * * *" or "*/30 * * * *"
    tz:       str | None = None # IANA timezone; None = UTC
    one_shot: bool = False      # if True, job auto-disables after firing once


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
    cursor_node_id:   str | None = None    # DB node_id for this job's branch cursor (isolated mode only)
    delete_after_run: bool       = False
    reset_after_run:  bool       = False   # wipe session context after each run (isolated mode only)
    run_in:           str        = "main"  # "main" (fork off the live channel tail) or "isolated" (private branch)
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
    try:
        # Aliased import — `croniter` as both module and class name confuses
        # static analysers if not aliased.
        from croniter import croniter as CronIter
        from zoneinfo import ZoneInfo
        tz   = ZoneInfo(schedule.tz) if schedule.tz else timezone.utc
        base = datetime.fromtimestamp(now_ms / 1000, tz=tz)
        nxt  = CronIter(schedule.expr, base).get_next(datetime)
        return int(nxt.timestamp() * 1000)
    except Exception:
        return None


def _validate_schedule(expr: str, tz: str | None) -> str | None:
    """Return an error string if the requested schedule is invalid, else None."""
    if not expr or not expr.strip():
        return "cron_expr is required."
    try:
        from croniter import croniter as CronIter
        if not CronIter.is_valid(expr):
            return f"invalid cron expression {expr!r}"
    except ImportError:
        return "croniter not installed — cron jobs are unavailable on this instance."
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
    schedule_expr     TEXT NOT NULL,
    schedule_tz       TEXT,
    schedule_one_shot INTEGER NOT NULL DEFAULT 0,
    message           TEXT NOT NULL,
    delete_after_run  INTEGER NOT NULL DEFAULT 0,
    reset_after_run   INTEGER NOT NULL DEFAULT 0,
    cursor_node_id    TEXT,
    run_in            TEXT NOT NULL DEFAULT 'main',
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
            expr=row["schedule_expr"],
            tz=row["schedule_tz"],
            one_shot=bool(row["schedule_one_shot"]),
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
        run_in=row["run_in"] if "run_in" in row.keys() else "main",
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
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(cron_jobs)")}
        if "run_in" not in cols:
            self._conn.execute(
                "ALTER TABLE cron_jobs ADD COLUMN run_in TEXT NOT NULL DEFAULT 'main'"
            )
        self._conn.commit()
        logger.info("[cron] store at %s", db_path)

    def add(self, job: CronJob) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO cron_jobs (
                    id, creator_username, platform, cursor_key, enabled,
                    schedule_expr, schedule_tz, schedule_one_shot,
                    message, delete_after_run, reset_after_run, cursor_node_id, run_in,
                    next_run_at_ms, last_run_at_ms, last_status, last_error,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id, job.creator_username, job.platform, job.cursor_key, int(job.enabled),
                    job.schedule.expr, job.schedule.tz, int(job.schedule.one_shot),
                    job.message, int(job.delete_after_run), int(job.reset_after_run), job.cursor_node_id, job.run_in,
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
    sched_str = f'"{s.expr}"'
    if s.tz:
        sched_str += f" ({s.tz})"
    if s.one_shot:
        sched_str += " [one-shot]"
    if j.run_in == "isolated":
        sched_str += " [isolated]"

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
                        if job.schedule.one_shot:
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

        logger.info("[cron] running job '%s' (creator=%s, run_in=%s, reset=%s)",
                     job.id, job.creator_username, job.run_in, job.reset_after_run)

        # 2. Determine the starting cursor.
        #
        # "main" (default): the job forks off the channel's own live tail at
        # fire time, exactly like a real message arriving in that channel —
        # done below by passing session_key=job.cursor_key to push(), which
        # attaches to Runtime._settled[cursor_key] (§3.2 concurrent forks).
        # tail_node_id here is only a *seed*, used the one time this
        # cursor_key has never been pushed to before; any other time it's
        # ignored in favor of the real settled tail. This is what makes the
        # agent's own past cron firings (and everyone else's live messages)
        # visible in this job's context, and vice versa — it's genuinely the
        # same conversation, not a side channel.
        #
        # "isolated": the job keeps its own private branch (job.cursor_node_id),
        # seeded from root and never touching the channel's live history —
        # same staleness guard as v1. reset_after_run only means anything here.
        if job.run_in == "isolated":
            if job.reset_after_run or not job.cursor_node_id or not self.runtime.db.get_node(job.cursor_node_id):
                if job.cursor_node_id and not job.reset_after_run:
                    logger.warning(
                        "[cron] job '%s' cursor %s no longer exists — resetting to root",
                        job.id, job.cursor_node_id,
                    )
                parent_id = self.runtime.db.get_root().id
            else:
                parent_id = job.cursor_node_id
        else:
            # main mode — seed only matters the first time cursor_key is seen.
            parent_id = self.runtime.db.get_root().id

        # 3. Prepare the turn — runs as the real creator, so permission-gated
        # tools see this job's actual current permission_level, not a fixed
        # system identity's.
        #
        # job.message is wrapped, not sent verbatim, and suppress_attribution
        # is set so this ONE node gets no 【label】: prefix — not the whole
        # cycle. A cron job's turn is still attached to whatever channel
        # history came before it (real people's messages, if reset_after_run
        # is False), and those must keep their own attribution; only the
        # single node this call writes should read as unattributed. See
        # InboundMessage.suppress_attribution / context.py's
        # NO_ATTRIBUTION_SENTINEL for how Runtime.push() and Context.assemble()
        # implement this per-node, not per-Context — author=creator below is
        # completely unaffected by suppress_attribution and still drives
        # caller.permission_level normally.
        #
        # Two complementary fixes for one root cause: without
        # suppress_attribution, the LLM would see what looks exactly like the
        # creator typing live (e.g. "【kamie】: Remind Alex to send the Q3
        # report") and naturally reply *to* them instead of carrying out the
        # instruction. Suppressing the prefix removes that misleading visual
        # cue; the wrapper text below makes the situation explicit in words
        # too, and tells the agent where its reply actually goes.
        wrapped_message = (
            "[Scheduled trigger — not a message from a person waiting in this "
            f"conversation. This fired automatically on the schedule you set up. "
            f"Your instruction to yourself was:]\n{job.message}\n\n"
            "[Carry it out now. Whatever you write in reply is what gets sent "
            "to this channel — there is no one here to reply \"to\".]"
        )
        msg = InboundMessage(
            tail_node_id=parent_id,
            author=creator,
            env=SessionEnvironment(platform=Platform.CRON),
            content_type=ContentType.TEXT,
            text=wrapped_message,
            message_id=str(start_ms),
            timestamp=start_ms / 1000,
            trigger=True,
            suppress_attribution=True,
        )

        reply_queue: asyncio.Queue = asyncio.Queue()

        try:
            # 4. Push — returns the user node id; events stream into reply_queue.
            #
            # main mode passes session_key=job.cursor_key so this run forks
            # off the channel's live settled tail (see the cursor-selection
            # comment above) and finish_run() advances that same tail
            # afterwards, exactly like a real bridge turn — no extra
            # bookkeeping needed here.
            #
            # isolated mode passes no session_key: a one-off internal
            # session (see push()'s docstring), invisible to the
            # concurrent-forks roster — matches v1's behavior exactly.
            if job.run_in == "main":
                await self.runtime.push(msg, reply_queue=reply_queue, session_key=job.cursor_key)
            else:
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

            if final_tail and job.run_in == "isolated":
                # main mode has no private cursor to persist — Runtime's own
                # _settled[cursor_key] already tracks the channel's tail.
                job.cursor_node_id = final_tail

            job.state.last_status = "ok"
            job.state.last_error = None

        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error("[cron] job '%s' failed: %s", job.id, e)

        # 6. Housekeeping.
        job.state.last_run_at_ms = start_ms
        if job.schedule.one_shot:
            job.enabled = False
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

        # delete_after_run removes the row outright rather than leaving it
        # disabled — meaningful on both one-shot jobs (skip the disabled
        # husk lingering in list_cron) and recurring jobs the agent wants
        # to fire exactly once more then forget.
        if job.delete_after_run:
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

    def add_cron(cron_expr: str, message: str, one_shot: bool = False,
                 tz: str = "", reset_after_run: bool = False, run_in: str = "main") -> str:
        """
        Schedule something to happen later, in this channel — a reminder,
        a recurring check-in, a daily/weekly report, anything you'd
        otherwise have to remember to do yourself.

        Use this whenever someone asks you to remind them of something
        later, or asks you to do something on a recurring basis (check
        in every morning, post a daily summary, follow up in a week,
        etc). You don't need to stay running for it to fire — it happens
        on its own, even if this conversation is long over.

        `message` is what YOU will receive when the schedule fires — not
        what gets shown to the person. Write it as an instruction to
        your future self: be specific and self-contained, since you
        won't have this conversation's context anymore. E.g. instead of
        "remind them", write "Remind Alex to send the Q3 report — she
        asked for a nudge at 3pm today." Whatever you say in response to
        that instruction is what the person in this channel actually sees.

        Scheduling is expressed as a standard 5-field cron expression
        (minute hour day-of-month month day-of-week), always in UTC
        unless you set tz. Examples:
          "0 9 * * *"     — every day at 9:00 UTC
          "*/30 * * * *"  — every 30 minutes
          "0 9 * * 1"     — every Monday at 9:00 UTC
          "0 17 * * 1-5"  — weekdays at 17:00 UTC

        For a one-time reminder ("remind me in 20 minutes", "follow up
        with them tomorrow at noon") rather than a recurring schedule:
        compute the single future UTC minute/hour/day/month you want
        (you have the current time in your context) and pass it as the
        cron expression with day-of-week as "*", e.g. current time
        14:10 UTC + 20 minutes -> "30 14 * * *", and set one_shot=True
        so it fires exactly once and then stops.

        By default the job fires right into this same conversation — when it
        goes off, it'll see everything that's happened here since (including
        its own past firings), and whatever you reply is just the next
        message in this channel. That's what you want for almost everything:
        reminders, check-ins, follow-ups.

        Set run_in="isolated" only if you specifically want the job to run
        in a private scratch conversation that nobody in this channel sees
        and that never mixes with this channel's history — useful for a
        background task whose reasoning shouldn't clutter this conversation
        (you'd still need another way to report results back). If you're not
        sure, leave it as "main".

        Args:
            cron_expr: 5-field cron expression — see examples above.
            message: The self-contained instruction you'll receive when this fires. Be detailed — you'll have no memory of this conversation.
            one_shot: True for a single reminder/follow-up that fires once and never again. False (default) for something recurring.
            tz: IANA timezone (e.g. "America/New_York") if the person means local time rather than UTC. Leave empty for UTC.
            reset_after_run: Only meaningful with run_in="isolated" — wipes that private scratch conversation after each run. No effect in the default "main" mode.
            run_in: "main" (default) to fire into this conversation, or "isolated" for a private one-off scratch conversation. Leave as "main" unless you have a specific reason not to.
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

        err = _validate_schedule(cron_expr, tz or None)
        if err:
            return json.dumps({"status": "error", "error": err})

        run_in = (run_in or "main").strip().lower()
        if run_in not in ("main", "isolated"):
            return json.dumps({"status": "error", "error": f"run_in must be 'main' or 'isolated', got {run_in!r}."})

        now = _now_ms()
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            creator_username=caller.username,
            platform=platform,
            cursor_key=cursor_key,
            enabled=True,
            schedule=CronSchedule(expr=cron_expr, tz=tz or None, one_shot=bool(one_shot)),
            message=message,
            delete_after_run=bool(one_shot),
            reset_after_run=bool(reset_after_run),
            run_in=run_in,
            created_at_ms=now,
            updated_at_ms=now,
        )
        job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
        store.add(job)
        logger.info("[cron] job '%s' created by %s in %s", job.id, caller.username, cursor_key)
        return json.dumps({"status": "ok", "job_id": job.id, "next_run": _fmt_ts(job.state.next_run_at_ms)})

    def list_cron() -> str:
        """
        Show everything currently scheduled in this channel — reminders,
        recurring check-ins, anything created with add_cron. Use this
        before scheduling something new if you're not sure whether a
        similar job already exists, or when someone asks what's still
        pending / what you've got scheduled.

        Only shows jobs scheduled in this channel — nothing from other
        conversations, even ones you're also active in.
        """
        channel = _current_channel()
        if channel is None:
            return "Could not determine this channel's identity."
        _, cursor_key = channel
        return _build_cron_list(store, cursor_key)

    def remove_cron(job_id: str) -> str:
        """
        Cancel a scheduled job — use when someone asks you to cancel a
        reminder or stop a recurring check-in. Call list_cron first to
        find the job's id if you don't already have it from when it was
        created.

        Only works on jobs in this channel, and only if you (the person
        who asked) created the job, or have permission to manage jobs
        others created here.

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

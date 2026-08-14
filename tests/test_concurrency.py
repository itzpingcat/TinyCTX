"""
tests/test_concurrency.py

Tests for Concurrent Forks (docs/PLAN.md) — the Runtime-side Run lifecycle,
the attach rule (R1), completion fan-out (R2), the inbox drain (R3), the
§5 completeness invariant, the §6 start/finish race, and §10/§11 scoping.

AgentCycle is never actually run here: runtime._process is replaced with a
controllable fake so each "run" finishes exactly when a test says so. That
keeps every assertion about the lifecycle logic itself, with no LLM in the
loop.

Run with:
    pytest tests/test_concurrency.py
"""
from __future__ import annotations

import asyncio
import time

import pytest

from TinyCTX.config.__main__ import (
    Config,
    DataConfig,
    LLMRoutingConfig,
    ModelConfig,
    WorkspaceConfig,
)
from TinyCTX.contracts import ContentType, InboundMessage, Platform, SessionEnvironment
from TinyCTX.runtime import Exogenous, Run, Runtime


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class _FakeUser:
    def __init__(self, username="tester", level=100):
        self.username = username
        self.permission_level = level
        self.user_id = "1"


@pytest.fixture
def config(tmp_path):
    return Config(
        models={"main": ModelConfig(model="m", base_url="http://x")},
        llm=LLMRoutingConfig(primary="main"),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        data=DataConfig(path=str(tmp_path / "data")),
    )


class FakeCycles:
    """
    Replaces Runtime._process. Each run parks until release(run_id) is called,
    then writes a head node off its root and calls finish_run — i.e. the same
    two things a real AgentCycle does that the lifecycle depends on.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.gates: dict[str, asyncio.Event] = {}
        self.heads: dict[str, str] = {}
        self.started: dict[str, asyncio.Event] = {}
        runtime._process = self._process  # type: ignore[method-assign]

    async def _process(self, run, caller, abort_event, reply_queue=None):
        gate = self.gates.setdefault(run.id, asyncio.Event())
        started = self.started.setdefault(run.id, asyncio.Event())
        started.set()
        try:
            await gate.wait()
            head = self.runtime.db.add_node(
                parent_id=run.root_node_id,
                role="assistant",
                content=f"reply from {run.id[:8]}",
            )
            self.heads[run.id] = head.id
            await self.runtime.finish_run(run, f"reply from {run.id[:8]}", head.id)
        finally:
            self.runtime._runs.pop(run.id, None)
            if reply_queue is not None:
                await reply_queue.put(None)

    async def release(self, run_id: str) -> None:
        """Let run_id finish, and wait until it has."""
        self.gates.setdefault(run_id, asyncio.Event()).set()
        for _ in range(100):
            if run_id in self.heads:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"run {run_id} did not finish")

    async def wait_started(self, run_id: str) -> None:
        ev = self.started.setdefault(run_id, asyncio.Event())
        await asyncio.wait_for(ev.wait(), timeout=1)


@pytest.fixture
def runtime(config):
    rt = Runtime(config)
    yield rt
    rt.db.close()


@pytest.fixture
def cycles(runtime):
    return FakeCycles(runtime)


def make_msg(text="hi", tail=None, trigger=True):
    return InboundMessage(
        tail_node_id=tail,
        author=_FakeUser(),
        env=SessionEnvironment(platform=Platform.DISCORD, agent_name="a"),
        content_type=ContentType.TEXT,
        text=text,
        message_id="m1",
        timestamp=time.time(),
        trigger=trigger,
    )


async def push(runtime, session_key, text="hi", tail=None, trigger=True):
    return await runtime.push(
        make_msg(text, tail=tail, trigger=trigger), session_key=session_key
    )


def only_run(runtime, session_key):
    runs = runtime.runs_in_session(session_key)
    assert len(runs) == 1, f"expected 1 run, got {len(runs)}"
    return runs[0]


def digests(run) -> list[str]:
    out = []
    while not run.inbox.empty():
        out.append(run.inbox.get_nowait())
    return out


# ---------------------------------------------------------------------------
# R1 — attach rule (§3.2)
# ---------------------------------------------------------------------------

class TestAttach:
    async def test_first_push_seeds_settled_tail_from_msg(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        assert runtime._settled["dm:1"] == root.id

    async def test_trigger_does_not_advance_settled_tail(self, runtime, cycles):
        """A running run's root must not become the attach point (§3.2)."""
        root = runtime.db.get_root()
        node_id = await push(runtime, "dm:1", tail=root.id)
        assert node_id != root.id
        assert runtime._settled["dm:1"] == root.id

    async def test_passive_message_advances_settled_tail(self, runtime, cycles):
        root = runtime.db.get_root()
        node_id = await push(runtime, "dm:1", tail=root.id, trigger=False)
        assert runtime._settled["dm:1"] == node_id
        assert runtime.runs_in_session("dm:1") == []

    async def test_concurrent_messages_fork_off_the_same_parent(self, runtime, cycles):
        """Two triggers while nothing has finished are siblings, not a chain."""
        root = runtime.db.get_root()
        a_node = await push(runtime, "dm:1", "first", tail=root.id)
        b_node = await push(runtime, "dm:1", "second", tail=root.id)

        assert runtime.db.get_node(a_node).parent_id == root.id
        assert runtime.db.get_node(b_node).parent_id == root.id
        assert len(runtime.runs_in_session("dm:1")) == 2

    async def test_message_after_finish_is_linear_continuation(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", "first", tail=root.id)
        run_a = only_run(runtime, "dm:1")
        await cycles.release(run_a.id)

        b_node = await push(runtime, "dm:1", "second", tail=root.id)
        assert runtime.db.get_node(b_node).parent_id == cycles.heads[run_a.id]

    async def test_settled_tail_is_per_session(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id, trigger=False)
        await push(runtime, "dm:2", tail=root.id, trigger=False)
        assert runtime._settled["dm:1"] != runtime._settled["dm:2"]


# ---------------------------------------------------------------------------
# R2 — completion fan-out
# ---------------------------------------------------------------------------

class TestFanOut:
    async def test_finish_advances_settled_tail_to_head(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        run = only_run(runtime, "dm:1")
        await cycles.release(run.id)
        assert runtime._settled["dm:1"] == cycles.heads[run.id]

    async def test_digest_reaches_running_peer(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", "first", tail=root.id)
        await push(runtime, "dm:1", "second", tail=root.id)
        run_a, run_b = runtime.runs_in_session("dm:1")

        await cycles.release(run_a.id)

        got = digests(run_b)
        assert len(got) == 1
        assert isinstance(got[0], Exogenous)
        assert got[0].kind == "fork_finished"
        assert run_a.id[:8] in got[0].content
        assert "first" in got[0].content

    async def test_run_never_receives_its_own_digest(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        run = only_run(runtime, "dm:1")
        await cycles.release(run.id)
        assert run.inbox.empty()

    async def test_fan_out_does_not_cross_sessions(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        await push(runtime, "dm:2", tail=root.id)
        run_a = only_run(runtime, "dm:1")
        run_b = only_run(runtime, "dm:2")

        await cycles.release(run_a.id)
        assert run_b.inbox.empty()

    async def test_finished_run_leaves_the_registry(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        run = only_run(runtime, "dm:1")
        await cycles.release(run.id)
        assert run.id not in runtime._runs
        assert run.status == "done"


# ---------------------------------------------------------------------------
# §5 — the completeness invariant
# ---------------------------------------------------------------------------

class TestCompleteness:
    async def test_last_finisher_has_everything(self, runtime, cycles):
        """A, B, C overlap; finish in order. C must hold A's and B's digests."""
        root = runtime.db.get_root()
        for text in ("a", "b", "c"):
            await push(runtime, "dm:1", text, tail=root.id)
        run_a, run_b, run_c = runtime.runs_in_session("dm:1")

        await cycles.release(run_a.id)
        await cycles.release(run_b.id)

        c_digests = digests(run_c)
        assert len(c_digests) == 2
        assert "'a'" in c_digests[0].content
        assert "'b'" in c_digests[1].content

        await cycles.release(run_c.id)
        assert runtime._settled["dm:1"] == cycles.heads[run_c.id]

    async def test_earlier_finisher_gets_nothing_after_it_left(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", "a", tail=root.id)
        await push(runtime, "dm:1", "b", tail=root.id)
        run_a, run_b = runtime.runs_in_session("dm:1")

        await cycles.release(run_a.id)
        await cycles.release(run_b.id)
        assert run_a.inbox.empty()

    async def test_non_overlapping_runs_inherit_by_ancestry(self, runtime, cycles):
        """No digest needed when the later run started after the earlier ended."""
        root = runtime.db.get_root()
        await push(runtime, "dm:1", "a", tail=root.id)
        run_a = only_run(runtime, "dm:1")
        await cycles.release(run_a.id)

        b_node = await push(runtime, "dm:1", "b", tail=root.id)
        run_b = only_run(runtime, "dm:1")

        assert run_b.inbox.empty()
        assert runtime.db.get_node(b_node).parent_id == cycles.heads[run_a.id]


# ---------------------------------------------------------------------------
# §6 — the start/finish race
# ---------------------------------------------------------------------------

class TestRace:
    async def test_push_blocks_while_session_lock_is_held(self, runtime, cycles):
        """Start transitions serialise against the same lock finish uses."""
        root = runtime.db.get_root()
        lock = runtime._session_lock("dm:1")
        await lock.acquire()

        task = asyncio.create_task(push(runtime, "dm:1", tail=root.id))
        await asyncio.sleep(0)
        assert not task.done()

        lock.release()
        await asyncio.wait_for(task, timeout=1)

    async def test_finish_blocks_while_session_lock_is_held(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        run = only_run(runtime, "dm:1")
        head = runtime.db.add_node(parent_id=run.root_node_id, role="assistant", content="x")

        lock = runtime._session_lock("dm:1")
        await lock.acquire()
        task = asyncio.create_task(runtime.finish_run(run, "x", head.id))
        await asyncio.sleep(0)
        assert not task.done()

        lock.release()
        await asyncio.wait_for(task, timeout=1)

    async def test_starter_either_inherits_or_is_fanned_to(self, runtime, cycles):
        """
        The §6 invariant: a run starting as another finishes never misses it —
        it either branches off the advanced tail, or receives the digest.
        """
        root = runtime.db.get_root()
        for _ in range(20):
            key = f"dm:race{_}"
            await push(runtime, key, "x", tail=root.id)
            run_x = only_run(runtime, key)
            await cycles.wait_started(run_x.id)

            finish = asyncio.create_task(cycles.release(run_x.id))
            start = asyncio.create_task(push(runtime, key, "y", tail=root.id))
            y_node, _ = await asyncio.gather(start, finish)

            run_y = only_run(runtime, key)
            inherited = runtime.db.get_node(y_node).parent_id == cycles.heads[run_x.id]
            fanned = any(run_x.id[:8] in d.content for d in digests(run_y))
            assert inherited or fanned, "run Y missed run X entirely"


# ---------------------------------------------------------------------------
# Digest rendering (§1, §12.3)
# ---------------------------------------------------------------------------

class TestDigest:
    def _run(self, intent="do the thing"):
        return Run(id="abcdef123456", session_key="dm:1", intent=intent, root_node_id="n1")

    def test_digest_contains_intent_and_final_text(self, runtime):
        out = runtime._render_digest(self._run(), "here is the answer")
        assert "abcdef12" in out
        assert "do the thing" in out
        assert "here is the answer" in out

    def test_digest_truncates_long_output(self, runtime):
        out = runtime._render_digest(self._run(), "x" * 5000)
        assert "...[truncated]" in out
        assert len(out) < 2500

    def test_digest_tolerates_empty_output(self, runtime):
        out = runtime._render_digest(self._run(), "")
        assert "abcdef12" in out


# ---------------------------------------------------------------------------
# §10 — roster scoping
# ---------------------------------------------------------------------------

class TestRosterScope:
    async def test_runs_in_session_filters_by_key(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        await push(runtime, "dm:2", tail=root.id)
        assert len(runtime.runs_in_session("dm:1")) == 1
        assert len(runtime.runs_in_session("dm:2")) == 1
        assert runtime.runs_in_session("dm:nope") == []

    async def test_callers_without_session_key_are_self_isolating(self, runtime, cycles):
        """
        Internal cycles (heartbeat/cron) push without a session_key, so they
        land in their own degenerate session and never pollute a real roster.
        """
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        await runtime.push(make_msg("heartbeat", tail=root.id))

        assert len(runtime.runs_in_session("dm:1")) == 1
        assert len(runtime.runs_in_session(root.id)) == 1

    async def test_spawned_fork_inherits_parent_session_key(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        parent = only_run(runtime, "dm:1")

        child = await runtime.start_run(
            "ignored-key", "sub task", parent.root_node_id, _FakeUser(), parent=parent
        )
        assert child.session_key == "dm:1"
        assert len(runtime.runs_in_session("dm:1")) == 2

    async def test_start_run_writes_branch_node_off_given_parent(self, runtime, cycles):
        root = runtime.db.get_root()
        run = await runtime.start_run("dm:1", "sub task", root.id, _FakeUser())
        node = runtime.db.get_node(run.root_node_id)
        assert node.parent_id == root.id
        assert node.content == "sub task"
        assert run.intent == "sub task"


# ---------------------------------------------------------------------------
# §11 — nudges
# ---------------------------------------------------------------------------

class TestNudge:
    async def test_nudge_reaches_a_running_peer(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", "a", tail=root.id)
        await push(runtime, "dm:1", "b", tail=root.id)
        run_a, run_b = runtime.runs_in_session("dm:1")

        assert runtime.nudge(run_b.id, run_a, "stop generating") is True
        got = digests(run_b)
        assert len(got) == 1
        assert got[0].kind == "nudge"
        assert "stop generating" in got[0].content

    async def test_nudge_is_marked_as_subordinate_to_the_user(self, runtime, cycles):
        """§11.2 — rendering carries authority."""
        root = runtime.db.get_root()
        await push(runtime, "dm:1", "a", tail=root.id)
        await push(runtime, "dm:1", "b", tail=root.id)
        run_a, run_b = runtime.runs_in_session("dm:1")

        runtime.nudge(run_b.id, run_a, "revert it")
        content = digests(run_b)[0].content
        assert "Not from the user" in content
        assert run_a.id[:8] in content

    async def test_nudge_across_sessions_is_refused(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        await push(runtime, "dm:2", tail=root.id)
        run_a = only_run(runtime, "dm:1")
        run_b = only_run(runtime, "dm:2")

        assert runtime.nudge(run_b.id, run_a, "hi") is False
        assert run_b.inbox.empty()

    async def test_nudge_to_finished_run_is_refused(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", "a", tail=root.id)
        await push(runtime, "dm:1", "b", tail=root.id)
        run_a, run_b = runtime.runs_in_session("dm:1")

        await cycles.release(run_b.id)
        assert runtime.nudge(run_b.id, run_a, "hi") is False

    async def test_nudge_to_unknown_run_is_refused(self, runtime, cycles):
        root = runtime.db.get_root()
        await push(runtime, "dm:1", tail=root.id)
        run = only_run(runtime, "dm:1")
        assert runtime.nudge("no-such-run", run, "hi") is False


# ---------------------------------------------------------------------------
# R3 — AgentCycle inbox drain
# ---------------------------------------------------------------------------

class _RecordingContext:
    def __init__(self):
        self.entries = []
        self.tail_node_id = "tail-0"

    def add(self, entry):
        self.entries.append(entry)
        self.tail_node_id = f"tail-{len(self.entries)}"
        return entry


class TestDrain:
    def _cycle(self, config):
        from TinyCTX.agent import AgentCycle

        cycle = AgentCycle(config, module_registry=None)
        cycle.context = _RecordingContext()
        cycle.active_run = Run(id="r1", session_key="dm:1", intent="i", root_node_id="n1")
        return cycle

    def test_drain_on_empty_inbox_reports_nothing_written(self, config):
        cycle = self._cycle(config)
        assert cycle._drain_inbox() is False
        assert cycle.context.entries == []

    def test_drain_writes_each_entry_as_a_node(self, config):
        cycle = self._cycle(config)
        cycle.active_run.inbox.put_nowait(Exogenous("fork_finished", "user", "fork 1 done"))
        cycle.active_run.inbox.put_nowait(Exogenous("nudge", "user", "please stop"))

        assert cycle._drain_inbox() is True
        assert [e.content for e in cycle.context.entries] == ["fork 1 done", "please stop"]
        assert [e.role for e in cycle.context.entries] == ["user", "user"]

    def test_drain_empties_the_inbox(self, config):
        cycle = self._cycle(config)
        cycle.active_run.inbox.put_nowait(Exogenous("nudge", "user", "x"))
        cycle._drain_inbox()
        assert cycle.active_run.inbox.empty()
        assert cycle._drain_inbox() is False

    def test_drain_advances_the_context_tail(self, config):
        cycle = self._cycle(config)
        cycle.active_run.inbox.put_nowait(Exogenous("nudge", "user", "x"))
        cycle._drain_inbox()
        assert cycle.context.tail_node_id == "tail-1"

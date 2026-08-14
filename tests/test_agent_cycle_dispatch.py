"""
tests/test_agent_cycle_dispatch.py

Regression tests for the *dispatch* path into AgentCycle — the seam
tests/test_concurrency.py deliberately stubs out ("AgentCycle is never
actually run here: runtime._process is replaced with a controllable fake").

That stub is why a real outage shipped green: AgentCycle.__init__ set
`self.run = None` to hold the live Run handle, and since functions are
non-data descriptors, the instance attribute shadowed the run() async
generator. Every `agent.run(...)` in Runtime._process raised
TypeError: 'NoneType' object is not callable, was swallowed by the broad
`except Exception`, and the bridge drained an empty queue and said nothing.

Three properties are locked down here:
  1. AgentCycle.run() survives __init__ — no instance attribute shadows it.
  2. Runtime._process makes a raising cycle *visible* on the reply_queue.
  3. A run that dies without finish_run still releases settled_tail.

Run with:
    pytest tests/test_agent_cycle_dispatch.py
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from TinyCTX.agent import AgentCycle
from TinyCTX.config.__main__ import (
    Config,
    DataConfig,
    LLMRoutingConfig,
    ModelConfig,
    WorkspaceConfig,
)
from TinyCTX.contracts import AgentError
from TinyCTX.runtime import Run, Runtime


class _FakeUser:
    username = "tester"
    permission_level = 100
    user_id = "1"


@pytest.fixture
def config(tmp_path):
    return Config(
        models={"main": ModelConfig(model="m", base_url="http://x")},
        llm=LLMRoutingConfig(primary="main"),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        data=DataConfig(path=str(tmp_path / "data")),
    )


@pytest.fixture
def runtime(config):
    rt = Runtime(config)
    yield rt
    rt.db.close()


# ---------------------------------------------------------------------------
# 1. No __init__ attribute may shadow a method
# ---------------------------------------------------------------------------

class TestNoMethodShadowing:
    def test_run_is_still_callable_after_init(self, config):
        cycle = AgentCycle(config, module_registry=None)
        assert callable(cycle.run), (
            "AgentCycle.run() is shadowed by an instance attribute — "
            "Runtime._process's agent.run(...) will raise TypeError and every "
            "turn will silently produce no reply."
        )
        assert inspect.isasyncgenfunction(cycle.run)

    def test_no_init_attribute_shadows_any_callable_class_attribute(self, config):
        """
        Generalises the specific bug: catches the next `self.<method_name> = x`
        in __init__, not just `self.run`.
        """
        cycle = AgentCycle(config, module_registry=None)
        shadowed = [
            name
            for name in vars(cycle)
            if callable(getattr(AgentCycle, name, None))
        ]
        assert not shadowed, (
            f"instance attribute(s) {shadowed} shadow AgentCycle methods of the "
            f"same name — calling them will fail at runtime"
        )

    def test_active_run_handle_round_trips(self, config):
        cycle = AgentCycle(config, module_registry=None)
        assert cycle.active_run is None
        run = Run(id="r1", session_key="dm:1", intent="i", root_node_id="n1")
        cycle.active_run = run
        assert cycle.active_run is run
        assert callable(cycle.run)  # still not clobbered


# ---------------------------------------------------------------------------
# 2. A crashing cycle must be visible, not silent
# ---------------------------------------------------------------------------

class TestProcessSurfacesFailure:
    async def test_raising_cycle_emits_agent_error_before_sentinel(
        self, runtime, monkeypatch
    ):
        class _Boom:
            def __init__(self, *a, **kw):
                pass

            def run(self, *a, **kw):
                async def _gen():
                    raise RuntimeError("kaboom")
                    yield  # pragma: no cover — makes this an async generator
                return _gen()

        monkeypatch.setattr("TinyCTX.agent.AgentCycle", _Boom)

        root = runtime.db.add_node(
            parent_id=runtime.db.get_root().id, role="user", content="hi"
        )
        run = Run(id="r1", session_key="dm:1", intent="i", root_node_id=root.id)
        runtime._runs[run.id] = run
        runtime._settled["dm:1"] = root.id

        q: asyncio.Queue = asyncio.Queue()
        await runtime._process(run, _FakeUser(), asyncio.Event(), q)

        events = []
        while not q.empty():
            events.append(q.get_nowait())

        assert events, "cycle failure produced no events at all — bridge stays silent"
        assert events[-1] is None, "completion sentinel must still be last"
        errors = [e for e in events if isinstance(e, AgentError)]
        assert errors, f"no AgentError emitted for a crashed cycle; got {events}"
        assert "kaboom" in errors[0].message

    async def test_failed_run_releases_settled_tail(self, runtime, monkeypatch):
        """
        finish_run never runs on the failure path, so without an explicit
        release settled_tail stays parked on the dead run's parent and every
        later message forks off the same stale node forever.
        """
        class _Boom:
            def __init__(self, *a, **kw):
                pass

            def run(self, *a, **kw):
                async def _gen():
                    raise RuntimeError("kaboom")
                    yield  # pragma: no cover
                return _gen()

        monkeypatch.setattr("TinyCTX.agent.AgentCycle", _Boom)

        parent = runtime.db.add_node(
            parent_id=runtime.db.get_root().id, role="user", content="hi"
        )
        root = runtime.db.add_node(parent_id=parent.id, role="user", content="go")
        run = Run(id="r1", session_key="dm:1", intent="i", root_node_id=root.id)
        runtime._runs[run.id] = run
        runtime._settled["dm:1"] = parent.id

        await runtime._process(run, _FakeUser(), asyncio.Event(), asyncio.Queue())

        assert runtime._settled["dm:1"] != parent.id, (
            "settled_tail still parked on the failed run's parent — the session "
            "will never advance again"
        )
        assert run.id not in runtime._runs

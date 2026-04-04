"""
tests/test_cron_heartbeat.py

Unit tests for modules/cron and modules/heartbeat.

Covers:
  - cron: schedule helpers (_compute_next_run, _validate_job)
  - cron: store load/save round-trip
  - cron: _CronRunner.start() registers the 'cron' platform sentinel
  - cron: _run_job happy path, timeout (abort called + no flood), cursor advance
  - cron: noop handler absorbs stray events after cursor handler unregistered
  - heartbeat: _parse_reply (ok / alert variants)
  - heartbeat: _in_active_window edge cases
  - heartbeat: _run_turn happy path, timeout (abort called)
  - heartbeat: register() wires up the loop task and reset hook

Run with:
    python -m pytest tests/test_cron_heartbeat.py -v
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _node_id() -> str:
    return str(uuid.uuid4())


def _make_config(tmp_path: Path):
    cfg = MagicMock()
    cfg.workspace.path = str(tmp_path)
    return cfg


# ===========================================================================
# CRON TESTS
# ===========================================================================

from modules.cron.__main__ import (
    CronJob, CronSchedule, CronState,
    _compute_next_run, _validate_job,
    _load_store, _save_store,
    _CronRunner, _noop_reply_handler,
    _CRON_PLATFORM,
)


# ---------------------------------------------------------------------------
# _compute_next_run
# ---------------------------------------------------------------------------

class TestComputeNextRun:
    def test_every_returns_now_plus_interval(self):
        s = CronSchedule(kind="every", every_ms=60_000)
        now = 1_000_000
        assert _compute_next_run(s, now) == now + 60_000

    def test_every_zero_ms_returns_none(self):
        s = CronSchedule(kind="every", every_ms=0)
        assert _compute_next_run(s, 1_000_000) is None

    def test_every_negative_ms_returns_none(self):
        s = CronSchedule(kind="every", every_ms=-1)
        assert _compute_next_run(s, 1_000_000) is None

    def test_at_future_returns_at_ms(self):
        now = 1_000_000
        s = CronSchedule(kind="at", at_ms=now + 5_000)
        assert _compute_next_run(s, now) == now + 5_000

    def test_at_past_returns_none(self):
        now = 1_000_000
        s = CronSchedule(kind="at", at_ms=now - 1)
        assert _compute_next_run(s, now) is None

    def test_at_exact_now_returns_none(self):
        now = 1_000_000
        s = CronSchedule(kind="at", at_ms=now)
        assert _compute_next_run(s, now) is None

    def test_unknown_kind_returns_none(self):
        s = CronSchedule(kind="bogus")
        assert _compute_next_run(s, 1_000_000) is None

    def test_cron_no_expr_returns_none(self):
        s = CronSchedule(kind="cron", expr=None)
        assert _compute_next_run(s, 1_000_000) is None


# ---------------------------------------------------------------------------
# _validate_job
# ---------------------------------------------------------------------------

def _job(schedule: CronSchedule, message="do stuff", enabled=True) -> CronJob:
    return CronJob(
        id="test-id",
        name="test",
        enabled=enabled,
        schedule=schedule,
        message=message,
    )


class TestValidateJob:
    def test_valid_every_no_warnings(self):
        j = _job(CronSchedule(kind="every", every_ms=60_000))
        assert _validate_job(j, 0) == []

    def test_every_zero_ms_warns(self):
        j = _job(CronSchedule(kind="every", every_ms=0))
        warnings = _validate_job(j, 0)
        assert any("every_ms" in w for w in warnings)

    def test_valid_at_future_no_warnings(self):
        j = _job(CronSchedule(kind="at", at_ms=9_999_999_999_999))
        assert _validate_job(j, 0) == []

    def test_at_past_warns_when_enabled(self):
        j = _job(CronSchedule(kind="at", at_ms=1), enabled=True)
        warnings = _validate_job(j, 1_000_000)
        assert any("past" in w for w in warnings)

    def test_at_past_no_warn_when_disabled(self):
        j = _job(CronSchedule(kind="at", at_ms=1), enabled=False)
        warnings = _validate_job(j, 1_000_000)
        assert not any("past" in w for w in warnings)

    def test_at_missing_at_ms_warns(self):
        j = _job(CronSchedule(kind="at", at_ms=None))
        warnings = _validate_job(j, 0)
        assert any("at_ms" in w for w in warnings)

    def test_empty_message_warns(self):
        j = _job(CronSchedule(kind="every", every_ms=1000), message="   ")
        warnings = _validate_job(j, 0)
        assert any("message" in w for w in warnings)

    def test_unknown_kind_warns(self):
        j = _job(CronSchedule(kind="unknown"))
        warnings = _validate_job(j, 0)
        assert any("unknown" in w for w in warnings)

    def test_cron_missing_expr_warns(self):
        j = _job(CronSchedule(kind="cron", expr=None))
        warnings = _validate_job(j, 0)
        assert any("expr" in w for w in warnings)


# ---------------------------------------------------------------------------
# Store round-trip
# ---------------------------------------------------------------------------

class TestStoreRoundTrip:
    def test_save_and_load(self, tmp_path):
        path = tmp_path / "CRON.json"
        jobs = [
            CronJob(
                id="abc1",
                name="test-job",
                enabled=True,
                schedule=CronSchedule(kind="every", every_ms=3_600_000),
                message="do something",
                state=CronState(next_run_at_ms=12345, last_run_at_ms=None),
                created_at_ms=1000,
                updated_at_ms=2000,
            )
        ]
        _save_store(path, jobs)
        loaded, version = _load_store(path)
        assert version == 1
        assert len(loaded) == 1
        j = loaded[0]
        assert j.id == "abc1"
        assert j.name == "test-job"
        assert j.schedule.kind == "every"
        assert j.schedule.every_ms == 3_600_000
        assert j.state.next_run_at_ms == 12345

    def test_load_missing_file_returns_empty(self, tmp_path):
        jobs, version = _load_store(tmp_path / "nonexistent.json")
        assert jobs == []
        assert version == 1

    def test_load_corrupt_file_returns_empty(self, tmp_path):
        path = tmp_path / "CRON.json"
        path.write_text("not valid json", encoding="utf-8")
        jobs, version = _load_store(path)
        assert jobs == []

    def test_round_trip_preserves_delete_after_run(self, tmp_path):
        path = tmp_path / "CRON.json"
        j = CronJob(
            id="x1", name="one-shot", enabled=True,
            schedule=CronSchedule(kind="at", at_ms=9_999_999_999_999),
            message="fire once", delete_after_run=True,
        )
        _save_store(path, [j])
        loaded, _ = _load_store(path)
        assert loaded[0].delete_after_run is True


# ---------------------------------------------------------------------------
# _noop_reply_handler
# ---------------------------------------------------------------------------

class TestNoopHandler:
    @pytest.mark.asyncio
    async def test_noop_accepts_anything_silently(self):
        # Should not raise, return value is not checked by callers.
        await _noop_reply_handler(MagicMock())
        await _noop_reply_handler(None)
        await _noop_reply_handler("some stray string event")


# ---------------------------------------------------------------------------
# _CronRunner.start() registers the cron platform sentinel
# ---------------------------------------------------------------------------

class TestCronRunnerRegistersHandler:
    def test_start_registers_noop_platform_handler_when_gateway_exists(self, tmp_path):
        """
        If agent.gateway already exists when start() runs, the cron platform
        sentinel should be registered immediately.
        """
        agent, gateway = _make_agent_and_gateway(tmp_path)

        # Confirm the slot starts empty.
        assert gateway._platform_handlers.get(_CRON_PLATFORM) is None

        runner = _CronRunner(agent, tmp_path / "CRON.json")
        runner.start()
        assert gateway._platform_handlers.get(_CRON_PLATFORM) is _noop_reply_handler

    @pytest.mark.asyncio
    async def test_noop_registered_before_first_job_events(self, tmp_path):
        """
        After the first _run_job() call the noop slot must be filled so that
        any stray events that arrive after unregister_cursor_handler don't
        produce 'no handler for platform cron' errors.
        """
        agent, gateway = _make_agent_and_gateway(tmp_path)
        gateway._platform_handlers = {}  # start clean
        job = _make_job()

        async def _fake_push(msg):
            from contracts import AgentTextFinal
            handler = gateway.register_cursor_handler.call_args[0][1]
            ev = MagicMock(spec=AgentTextFinal)
            ev.text = "done"
            await handler(ev)

        gateway.push = _fake_push

        runner = _CronRunner(agent, tmp_path / "CRON.json")
        await runner._run_job(job)

        assert gateway._platform_handlers.get(_CRON_PLATFORM) is _noop_reply_handler

    def test_start_without_gateway_does_not_crash(self, tmp_path):
        """start() must not raise even when gateway isn't set yet (normal case)."""
        agent = MagicMock()
        agent.config.workspace.path = str(tmp_path)
        del agent.gateway

        runner = _CronRunner(agent, tmp_path / "CRON.json")
        runner.start()  # should not raise


# ---------------------------------------------------------------------------
# _CronRunner._run_job
# ---------------------------------------------------------------------------

def _make_agent_and_gateway(tmp_path):
    """Return (agent, gateway) with just enough wiring for _run_job tests."""
    agent   = MagicMock()
    gateway = MagicMock()
    agent.config.workspace.path = str(tmp_path)
    agent.gateway = gateway

    # gateway.push is async
    gateway.push = AsyncMock()
    gateway.register_cursor_handler = MagicMock()
    gateway.unregister_cursor_handler = MagicMock()
    gateway.abort_generation = MagicMock()
    gateway.reset_lane = MagicMock()
    gateway._platform_handlers = {}
    gateway._lane_router = MagicMock()
    gateway._lane_router._lanes = {}
    return agent, gateway


def _make_job(kind="every", every_ms=60_000, message="ping") -> CronJob:
    return CronJob(
        id=str(uuid.uuid4())[:8],
        name="test-job",
        enabled=True,
        schedule=CronSchedule(kind=kind, every_ms=every_ms),
        message=message,
    )


class TestRunJob:
    @pytest.mark.asyncio
    async def test_happy_path_sets_status_ok(self, tmp_path):
        agent, gateway = _make_agent_and_gateway(tmp_path)
        job = _make_job()

        async def _fake_push(msg):
            # Simulate agent emitting AgentTextFinal which sets reply_event
            # We reach into the runner to get the collect handler and call it.
            from contracts import AgentTextFinal
            handler = gateway.register_cursor_handler.call_args[0][1]
            fake_event = MagicMock(spec=AgentTextFinal)
            fake_event.text = "done"
            await handler(fake_event)

        gateway.push = _fake_push

        runner = _CronRunner(agent, tmp_path / "CRON.json")
        await runner._run_job(job)

        assert job.state.last_status == "ok"
        assert job.state.last_error is None
        assert job.state.last_run_at_ms is not None

    @pytest.mark.asyncio
    async def test_timeout_sets_error_status_and_calls_abort(self, tmp_path):
        """
        Fix verification: on timeout, abort_generation must be called
        before unregister_cursor_handler so the lane stops emitting events.
        """
        agent, gateway = _make_agent_and_gateway(tmp_path)
        job = _make_job()

        # push() does nothing → reply_event never set → timeout fires
        gateway.push = AsyncMock()

        runner = _CronRunner(agent, tmp_path / "CRON.json")

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            await runner._run_job(job)

        assert job.state.last_status == "error"
        assert "timed out" in (job.state.last_error or "")

        # abort must have been called
        gateway.abort_generation.assert_called_once()

        # abort must be called BEFORE unregister
        abort_idx = next(
            i for i, c in enumerate(gateway.method_calls)
            if c == call.abort_generation(gateway.abort_generation.call_args[0][0])
        )
        unreg_idx = next(
            i for i, c in enumerate(gateway.method_calls)
            if "unregister_cursor_handler" in str(c)
        )
        assert abort_idx < unreg_idx, "abort_generation must precede unregister_cursor_handler"

    @pytest.mark.asyncio
    async def test_cursor_advances_after_turn(self, tmp_path):
        """After a successful turn the job's cursor_node_id advances to the lane tail."""
        agent, gateway = _make_agent_and_gateway(tmp_path)
        job = _make_job()
        new_tail = _node_id()

        async def _fake_push(msg):
            from contracts import AgentTextFinal
            handler = gateway.register_cursor_handler.call_args[0][1]
            fake_event = MagicMock(spec=AgentTextFinal)
            fake_event.text = "done"
            await handler(fake_event)

        gateway.push = _fake_push

        # Simulate the lane having advanced its tail
        fake_lane = MagicMock()
        fake_lane.loop._tail_node_id = new_tail

        runner = _CronRunner(agent, tmp_path / "CRON.json")
        await runner._run_job(job)

        # Register the fake lane so cursor advance is exercised
        old_cursor = job.cursor_node_id
        gateway._lane_router._lanes[old_cursor] = fake_lane

        # Run a second time — cursor should pick up new_tail
        await runner._run_job(job)
        assert job.cursor_node_id == new_tail

    @pytest.mark.asyncio
    async def test_no_gateway_sets_error(self, tmp_path):
        agent = MagicMock()
        agent.config.workspace.path = str(tmp_path)
        del agent.gateway

        job = _make_job()
        runner = _CronRunner(agent, tmp_path / "CRON.json")
        await runner._run_job(job)

        assert job.state.last_status == "error"

    @pytest.mark.asyncio
    async def test_at_job_disabled_after_run(self, tmp_path):
        agent, gateway = _make_agent_and_gateway(tmp_path)
        job = _make_job(kind="at")
        job.schedule.at_ms = int(time.time() * 1000) + 100_000
        job.state.next_run_at_ms = int(time.time() * 1000) - 1

        async def _fake_push(msg):
            from contracts import AgentTextFinal
            handler = gateway.register_cursor_handler.call_args[0][1]
            ev = MagicMock(spec=AgentTextFinal)
            ev.text = "ok"
            await handler(ev)

        gateway.push = _fake_push
        runner = _CronRunner(agent, tmp_path / "CRON.json")
        await runner._run_job(job)

        assert job.enabled is False
        assert job.state.next_run_at_ms is None

    @pytest.mark.asyncio
    async def test_stray_events_after_unregister_absorbed_by_noop(self, tmp_path):
        """
        Regression: events that arrive after cursor handler is unregistered
        must go to the noop platform handler, not produce 'no handler' errors.
        """
        agent, gateway = _make_agent_and_gateway(tmp_path)

        # Use a real Router to verify the noop slot is actually wired.
        from router import Router
        real_gateway = Router(config=_make_config(tmp_path))
        agent.gateway = real_gateway

        # Start runner — this should register the noop platform handler.
        runner = _CronRunner(agent, tmp_path / "CRON.json")
        runner.start()

        # Confirm the slot is filled.
        handler = real_gateway._platform_handlers.get(_CRON_PLATFORM)
        assert handler is not None, "noop handler must be registered for 'cron' platform"

        # Calling it must not raise.
        await handler(MagicMock())


# ===========================================================================
# HEARTBEAT TESTS
# ===========================================================================

from modules.heartbeat.__main__ import (
    _parse_reply,
    _in_active_window,
    _run_turn,
    _HEARTBEAT_AUTHOR,
)


# ---------------------------------------------------------------------------
# _parse_reply
# ---------------------------------------------------------------------------

class TestParseReply:
    def test_exact_token_is_ok(self):
        is_ok, remainder = _parse_reply("HEARTBEAT_OK", ack_max=300)
        assert is_ok is True
        assert remainder == ""

    def test_token_at_start_stripped(self):
        is_ok, remainder = _parse_reply("HEARTBEAT_OK\nSome detail", ack_max=300)
        assert is_ok is True
        assert remainder == "Some detail"

    def test_token_at_end_stripped(self):
        is_ok, remainder = _parse_reply("Some detail\nHEARTBEAT_OK", ack_max=300)
        assert is_ok is True
        assert remainder == "Some detail"

    def test_long_remainder_not_ok(self):
        long_text = "x" * 301
        is_ok, _ = _parse_reply(f"HEARTBEAT_OK\n{long_text}", ack_max=300)
        assert is_ok is False

    def test_no_token_not_ok(self):
        is_ok, text = _parse_reply("I did some stuff", ack_max=300)
        assert is_ok is False
        assert text == "I did some stuff"

    def test_empty_string_is_ok(self):
        is_ok, _ = _parse_reply("", ack_max=300)
        assert is_ok is True

    def test_ack_max_zero_only_empty_is_ok(self):
        is_ok, _ = _parse_reply("HEARTBEAT_OK", ack_max=0)
        assert is_ok is True
        is_ok2, _ = _parse_reply("HEARTBEAT_OK\na", ack_max=0)
        assert is_ok2 is False


# ---------------------------------------------------------------------------
# _in_active_window
# ---------------------------------------------------------------------------

class TestInActiveWindow:
    def test_none_always_active(self):
        assert _in_active_window(None) is True

    def test_inside_window(self):
        from datetime import datetime, time as dtime
        now = datetime.now().time()
        # Build a window that definitely contains now
        start = dtime(0, 0)
        end_  = dtime(23, 59)
        assert _in_active_window({"start": "00:00", "end": "23:59"}) is True

    def test_outside_window(self):
        # Use a window entirely in the past hour 00:00-00:01
        # unless it's actually midnight — this is a best-effort test
        from datetime import datetime
        now_hour = datetime.now().hour
        if now_hour == 0:
            pytest.skip("Skipping edge case at midnight")
        assert _in_active_window({"start": "00:00", "end": "00:01"}) is False

    def test_equal_start_end_is_never_active(self):
        assert _in_active_window({"start": "12:00", "end": "12:00"}) is False

    def test_invalid_config_defaults_to_active(self):
        assert _in_active_window({"start": "bad"}) is True
        assert _in_active_window({}) is True


# ---------------------------------------------------------------------------
# _run_turn
# ---------------------------------------------------------------------------

class TestRunTurn:
    @pytest.mark.asyncio
    async def test_happy_path_returns_reply(self, tmp_path):
        agent = MagicMock()
        agent.config.workspace.path = str(tmp_path)
        gateway = MagicMock()
        agent.gateway = gateway
        gateway.push = AsyncMock()
        gateway.register_cursor_handler = MagicMock()
        gateway.unregister_cursor_handler = MagicMock()
        gateway.abort_generation = MagicMock()
        gateway._lane_router._lanes = {}

        lane_node_id = _node_id()
        tail_node_id = _node_id()

        async def _fake_push(msg):
            from contracts import AgentTextFinal
            handler = gateway.register_cursor_handler.call_args[0][1]
            ev = MagicMock(spec=AgentTextFinal)
            ev.text = "HEARTBEAT_OK"
            await handler(ev)

        gateway.push = _fake_push

        reply, new_tail = await _run_turn(agent, lane_node_id, tail_node_id, "tick")
        assert reply == "HEARTBEAT_OK"

    @pytest.mark.asyncio
    async def test_timeout_calls_abort_then_unregister(self, tmp_path):
        """
        Fix verification: on timeout, abort_generation must be called
        before cursor handler is unregistered.
        """
        agent = MagicMock()
        agent.config.workspace.path = str(tmp_path)
        gateway = MagicMock()
        agent.gateway = gateway
        gateway.push = AsyncMock()
        gateway.register_cursor_handler = MagicMock()
        gateway.unregister_cursor_handler = MagicMock()
        gateway.abort_generation = MagicMock()
        gateway._lane_router._lanes = {}

        lane_node_id = _node_id()

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            reply, _ = await _run_turn(agent, lane_node_id, lane_node_id, "tick")

        assert reply == ""
        gateway.abort_generation.assert_called_once_with(lane_node_id)

        # abort before unregister
        abort_idx = next(
            i for i, c in enumerate(gateway.method_calls)
            if "abort_generation" in str(c)
        )
        unreg_idx = next(
            i for i, c in enumerate(gateway.method_calls)
            if "unregister_cursor_handler" in str(c)
        )
        assert abort_idx < unreg_idx

    @pytest.mark.asyncio
    async def test_no_gateway_returns_empty(self, tmp_path):
        agent = MagicMock()
        agent.config.workspace.path = str(tmp_path)
        del agent.gateway

        reply, _ = await _run_turn(agent, _node_id(), _node_id(), "tick")
        assert reply == ""

    @pytest.mark.asyncio
    async def test_agent_error_event_sets_reply(self, tmp_path):
        agent = MagicMock()
        gateway = MagicMock()
        agent.gateway = gateway
        gateway.register_cursor_handler = MagicMock()
        gateway.unregister_cursor_handler = MagicMock()
        gateway.abort_generation = MagicMock()
        gateway._lane_router._lanes = {}

        async def _fake_push(msg):
            from contracts import AgentError
            handler = gateway.register_cursor_handler.call_args[0][1]
            ev = MagicMock(spec=AgentError)
            ev.message = "LLM error"
            await handler(ev)

        gateway.push = _fake_push
        reply, _ = await _run_turn(agent, _node_id(), _node_id(), "tick")
        assert reply == "LLM error"


# ---------------------------------------------------------------------------
# register() wires reset hook
# ---------------------------------------------------------------------------

class TestHeartbeatRegister:
    @pytest.mark.asyncio
    async def test_reset_cancels_task(self, tmp_path):
        from modules.heartbeat.__main__ import register, _patch_reset

        agent = MagicMock()
        agent.config.workspace.path = str(tmp_path)
        agent.reset = MagicMock()
        agent._heartbeat_lane_node_id = None
        agent._heartbeat_cursor_node_id = None
        agent._tail_node_id = _node_id()
        agent.gateway = None

        fake_task = MagicMock()
        fake_task.done.return_value = False

        _patch_reset(agent, fake_task)
        agent.reset()

        fake_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_skips_cancel_if_task_done(self, tmp_path):
        from modules.heartbeat.__main__ import _patch_reset

        agent = MagicMock()
        agent.reset = MagicMock()

        fake_task = MagicMock()
        fake_task.done.return_value = True

        _patch_reset(agent, fake_task)
        agent.reset()

        fake_task.cancel.assert_not_called()

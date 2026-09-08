"""
tests/test_hooks.py — unit tests for TinyCTX/hooks.py (HookRegistry, HookType,
Combine, Scratch). See docs/MODULES-PLAN-P1.md.

These test the registry in isolation, not wired into Context/AgentCycle yet
(that wiring is a separate, later step — see MODULES-PLAN-P1.md's P1 phase 2).
"""
from __future__ import annotations

import pytest

from TinyCTX.hooks import Combine, HookRegistry, HookType, Scratch


class TestHookTypeEnum:
    def test_wire_names_are_unique(self):
        names = [t.wire_name for t in HookType]
        assert len(names) == len(set(names))

    def test_from_wire_name_resolves_known_stage(self):
        assert HookType.from_wire_name("transform_turn") is HookType.TRANSFORM_TURN

    def test_from_wire_name_raises_on_unknown_stage(self):
        with pytest.raises(ValueError):
            HookType.from_wire_name("pre_assemble_asycn")  # typo, on purpose

    def test_stream_text_is_not_isolated(self):
        assert HookType.STREAM_TEXT.isolate is False

    def test_everything_else_is_isolated_by_default(self):
        for t in HookType:
            if t is HookType.STREAM_TEXT:
                continue
            assert t.isolate is True, f"{t} should default isolate=True"

    def test_combine_strategy_is_fixed_per_type(self):
        assert HookType.FILTER_TURN.combine is Combine.VETO
        assert HookType.TRANSFORM_TURN.combine is Combine.CHAIN
        assert HookType.POST_COMPLETION.combine is Combine.COLLECT
        assert HookType.STARTUP.combine is Combine.FANOUT
        assert HookType.DELIVER.combine is Combine.DISPATCH


class TestRegistration:
    def test_register_rejects_non_hooktype(self):
        reg = HookRegistry()
        with pytest.raises(TypeError):
            reg.register("transform_turn", lambda *a: None)  # a string, not HookType

    def test_register_orders_by_priority_then_insertion(self):
        reg = HookRegistry()
        calls = []
        reg.register(HookType.STARTUP, lambda: calls.append("b"), priority=5)
        reg.register(HookType.STARTUP, lambda: calls.append("a"), priority=0)
        reg.register(HookType.STARTUP, lambda: calls.append("c"), priority=5)
        assert [fn.__name__ if hasattr(fn, "__name__") else fn for fn in reg.handlers_for(HookType.STARTUP)]
        # Run in emit() order via FANOUT
        import asyncio
        asyncio.run(reg.emit(HookType.STARTUP))
        assert calls == ["a", "b", "c"]

    def test_unregister_removes_handler(self):
        reg = HookRegistry()
        fn = lambda: None
        reg.register(HookType.STARTUP, fn)
        reg.unregister(HookType.STARTUP, fn)
        assert reg.handlers_for(HookType.STARTUP) == []


class TestCombineFanout:
    @pytest.mark.asyncio
    async def test_fanout_runs_all_and_returns_none(self):
        reg = HookRegistry()
        seen = []
        reg.register(HookType.STARTUP, lambda: seen.append(1))
        reg.register(HookType.STARTUP, lambda: seen.append(2))
        result = await reg.emit(HookType.STARTUP)
        assert result is None
        assert seen == [1, 2]

    @pytest.mark.asyncio
    async def test_fanout_isolates_a_raising_handler(self):
        reg = HookRegistry()
        seen = []

        def bad():
            raise RuntimeError("boom")

        reg.register(HookType.STARTUP, bad, priority=0)
        reg.register(HookType.STARTUP, lambda: seen.append("ok"), priority=1)
        await reg.emit(HookType.STARTUP)  # must not raise
        assert seen == ["ok"]


class TestCombineVeto:
    @pytest.mark.asyncio
    async def test_veto_true_when_nothing_vetoes(self):
        reg = HookRegistry()
        reg.register(HookType.FILTER_TURN, lambda entry, age, ctx=None: True)
        result = await reg.emit(HookType.FILTER_TURN, "entry", 0)
        assert result is True

    @pytest.mark.asyncio
    async def test_veto_false_short_circuits(self):
        reg = HookRegistry()
        calls = []

        def first(entry, age):
            calls.append("first")
            return False

        def second(entry, age):
            calls.append("second")  # pragma: no cover - must not run
            return True

        reg.register(HookType.FILTER_TURN, first, priority=0)
        reg.register(HookType.FILTER_TURN, second, priority=1)
        result = await reg.emit(HookType.FILTER_TURN, "entry", 0)
        assert result is False
        assert calls == ["first"]


class TestCombineChain:
    @pytest.mark.asyncio
    async def test_chain_threads_value_through_handlers(self):
        reg = HookRegistry()
        reg.register(HookType.TRANSFORM_TURN, lambda v, age, ctx=None: v + 1, priority=0)
        reg.register(HookType.TRANSFORM_TURN, lambda v, age, ctx=None: v * 10, priority=1)
        result = await reg.emit(HookType.TRANSFORM_TURN, 1, 0)
        assert result == 20  # (1 + 1) * 10

    @pytest.mark.asyncio
    async def test_chain_none_return_leaves_value_unchanged(self):
        reg = HookRegistry()
        reg.register(HookType.TRANSFORM_TURN, lambda v, age: None, priority=0)
        reg.register(HookType.TRANSFORM_TURN, lambda v, age: v + 1, priority=1)
        result = await reg.emit(HookType.TRANSFORM_TURN, 1, 0)
        assert result == 2

    @pytest.mark.asyncio
    async def test_chain_requires_at_least_one_arg(self):
        reg = HookRegistry()
        with pytest.raises(TypeError):
            await reg.emit(HookType.TRANSFORM_TURN)


class TestCombineCollect:
    @pytest.mark.asyncio
    async def test_collect_gathers_non_none_returns(self):
        reg = HookRegistry()
        reg.register(HookType.POST_COMPLETION, lambda text, calls: "a")
        reg.register(HookType.POST_COMPLETION, lambda text, calls: None)
        reg.register(HookType.POST_COMPLETION, lambda text, calls: "b")
        result = await reg.emit(HookType.POST_COMPLETION, "hi", [])
        assert result == ["a", "b"]


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_looks_up_by_key_not_iteration(self):
        reg = HookRegistry()
        calls = []
        reg.register_dispatch(HookType.DELIVER, "discord", lambda dest, event: calls.append(("discord", dest, event)))
        reg.register_dispatch(HookType.DELIVER, "telegram", lambda dest, event: calls.append(("telegram", dest, event)))
        await reg.emit_dispatch(HookType.DELIVER, "discord", "chan1", "ev")
        assert calls == [("discord", "chan1", "ev")]

    @pytest.mark.asyncio
    async def test_dispatch_missing_key_returns_none(self):
        reg = HookRegistry()
        result = await reg.emit_dispatch(HookType.DELIVER, "nope", "dest", "ev")
        assert result is None

    @pytest.mark.asyncio
    async def test_emit_rejects_dispatch_type(self):
        reg = HookRegistry()
        with pytest.raises(TypeError):
            await reg.emit(HookType.DELIVER, "dest", "ev")


class TestAsyncHandlers:
    @pytest.mark.asyncio
    async def test_coroutine_handlers_are_awaited(self):
        reg = HookRegistry()
        seen = []

        async def handler():
            seen.append("async")

        reg.register(HookType.STARTUP, handler)
        await reg.emit(HookType.STARTUP)
        assert seen == ["async"]

    @pytest.mark.asyncio
    async def test_mixed_sync_and_async_handlers(self):
        reg = HookRegistry()
        seen = []

        async def a():
            seen.append("a")

        def b():
            seen.append("b")

        reg.register(HookType.STARTUP, a, priority=0)
        reg.register(HookType.STARTUP, b, priority=1)
        await reg.emit(HookType.STARTUP)
        assert seen == ["a", "b"]


class TestScratch:
    def test_attributes_settable_and_readable(self):
        s = Scratch()
        s.suppressed = {1, 2}
        assert s.suppressed == {1, 2}

    def test_unset_attribute_raises(self):
        s = Scratch()
        with pytest.raises(AttributeError):
            _ = s.never_set

    def test_two_instances_do_not_share_state(self):
        s1 = Scratch()
        s2 = Scratch()
        s1.x = 1
        assert not hasattr(s2, "x")


class TestScratchInjection:
    @pytest.mark.asyncio
    async def test_handler_declaring_scratch_param_receives_it(self):
        reg = HookRegistry()
        scratch = Scratch()
        received = {}

        def handler(ctx, scratch):
            received["scratch"] = scratch

        reg.register(HookType.PRE_ASSEMBLE, handler)
        await reg.emit(HookType.PRE_ASSEMBLE, "ctx", scratch=scratch)
        assert received["scratch"] is scratch

    @pytest.mark.asyncio
    async def test_handler_without_scratch_param_is_unaffected(self):
        reg = HookRegistry()
        scratch = Scratch()
        seen = []
        reg.register(HookType.PRE_ASSEMBLE, lambda ctx: seen.append(ctx))
        await reg.emit(HookType.PRE_ASSEMBLE, "ctx", scratch=scratch)
        assert seen == ["ctx"]


class TestStreamTextFastPath:
    def test_emit_stream_text_sync_pipes_through_handlers_in_order(self):
        reg = HookRegistry()
        reg.register(HookType.STREAM_TEXT, lambda t: t.upper(), priority=0)
        reg.register(HookType.STREAM_TEXT, lambda t: t + "!", priority=1)
        result = reg.emit_stream_text_sync("hi")
        assert result == "HI!"

    def test_emit_stream_text_sync_propagates_raising_handler(self):
        reg = HookRegistry()

        def bad(t):
            raise RuntimeError("boom")

        reg.register(HookType.STREAM_TEXT, bad)
        with pytest.raises(RuntimeError):
            reg.emit_stream_text_sync("hi")


class TestHookListProxy:
    def test_append_and_iterate_preserves_order(self):
        reg = HookRegistry()
        from TinyCTX.hooks import HookListProxy
        proxy = HookListProxy(reg, HookType.POST_TURN)
        proxy.append("a")
        proxy.append("b")
        proxy.append("c")
        assert list(proxy) == ["a", "b", "c"]

    def test_len_reflects_registered_count(self):
        reg = HookRegistry()
        from TinyCTX.hooks import HookListProxy
        proxy = HookListProxy(reg, HookType.POST_TURN)
        assert len(proxy) == 0
        proxy.append(lambda: None)
        assert len(proxy) == 1

    def test_appended_handlers_are_visible_via_the_underlying_registry(self):
        reg = HookRegistry()
        from TinyCTX.hooks import HookListProxy
        proxy = HookListProxy(reg, HookType.POST_TURN)
        fn = lambda: None
        proxy.append(fn)
        assert reg.handlers_for(HookType.POST_TURN) == [fn]

    def test_two_proxies_on_different_types_do_not_collide(self):
        reg = HookRegistry()
        from TinyCTX.hooks import HookListProxy
        post_turn = HookListProxy(reg, HookType.POST_TURN)
        startup = HookListProxy(reg, HookType.STARTUP)
        post_turn.append("pt")
        startup.append("su")
        assert list(post_turn) == ["pt"]
        assert list(startup) == ["su"]

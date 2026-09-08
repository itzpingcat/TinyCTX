"""
tests/test_platform_delivery.py

Tests for Runtime.register_platform_handler() / Runtime.deliver() —
docs/MODULES-PLAN-P1.md's "_platform_handlers becomes DELIVER" step. This
had no dedicated test coverage before that migration; added here as part of
moving its storage onto HookRegistry (HookType.DELIVER, Combine.DISPATCH)
while keeping the public methods' exact prior behavior: overwrite-on-
re-register, "no handler" vs "handler raised" distinguished with their own
specific log messages and return values.

Run with:
    pytest tests/test_platform_delivery.py -v
"""
from __future__ import annotations

import pytest

from TinyCTX.config.__main__ import (
    Config,
    DataConfig,
    LLMRoutingConfig,
    ModelConfig,
    WorkspaceConfig,
)
from TinyCTX.runtime import Runtime
from TinyCTX.hooks import HookType


@pytest.fixture
def config(tmp_path):
    return Config(
        models={"main": ModelConfig(model="m", base_url="http://x")},
        llm=LLMRoutingConfig(primary="main"),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        data=DataConfig(path=str(tmp_path / "data")),
    )


@pytest.fixture
def rt(config):
    return Runtime(config)


class TestRegisterPlatformHandler:
    def test_registers_and_is_visible_via_hookregistry(self, rt):
        async def handler(dest, event):
            pass

        rt.register_platform_handler("discord", handler)
        assert rt._hooks.dispatch_handler_for(HookType.DELIVER, "discord") is handler

    def test_reregistering_overwrites(self, rt):
        async def first(dest, event):
            pass

        async def second(dest, event):
            pass

        rt.register_platform_handler("discord", first)
        rt.register_platform_handler("discord", second)
        assert rt._hooks.dispatch_handler_for(HookType.DELIVER, "discord") is second


class TestDeliver:
    @pytest.mark.asyncio
    async def test_delivers_to_registered_handler_and_returns_true(self, rt):
        calls = []

        async def handler(dest, event):
            calls.append((dest, event))

        rt.register_platform_handler("discord", handler)
        result = await rt.deliver("discord", "chan1", "the-event")
        assert result is True
        assert calls == [("chan1", "the-event")]

    @pytest.mark.asyncio
    async def test_missing_handler_returns_false_without_raising(self, rt):
        result = await rt.deliver("nope", "dest", "ev")
        assert result is False

    @pytest.mark.asyncio
    async def test_raising_handler_is_caught_and_returns_false(self, rt):
        async def bad(dest, event):
            raise RuntimeError("boom")

        rt.register_platform_handler("discord", bad)
        result = await rt.deliver("discord", "chan1", "ev")
        assert result is False

    @pytest.mark.asyncio
    async def test_two_platforms_are_independently_dispatched(self, rt):
        calls = []

        async def discord_handler(dest, event):
            calls.append(("discord", dest, event))

        async def telegram_handler(dest, event):
            calls.append(("telegram", dest, event))

        rt.register_platform_handler("discord", discord_handler)
        rt.register_platform_handler("telegram", telegram_handler)
        await rt.deliver("discord", "d1", "ev1")
        await rt.deliver("telegram", "t1", "ev2")
        assert calls == [("discord", "d1", "ev1"), ("telegram", "t1", "ev2")]

"""
tests/test_commands.py

Tests for utils/commands.py — CommandRegistry, the slash-command dispatch
registry used by bridges before pushing text to the router.

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.utils.commands import CommandRegistry


@pytest.fixture
def registry():
    return CommandRegistry()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_and_dispatch_bare_namespace(self, registry):
        seen = []

        async def handler(args, context):
            seen.append((args, context))

        registry.register("memory", "", handler, help="do memory stuff")
        handled = await registry.dispatch("/memory", {})
        assert handled is True
        assert seen == [([], {})]

    @pytest.mark.asyncio
    async def test_register_with_subcommand(self, registry):
        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("memory", "consolidate", handler)
        handled = await registry.dispatch("/memory consolidate", {})
        assert handled is True
        assert seen == [[]]

    def test_re_registering_same_namespace_sub_replaces(self, registry):
        async def h1(args, context):
            pass

        async def h2(args, context):
            pass

        registry.register("ns", "sub", h1, help="first")
        registry.register("ns", "sub", h2, help="second")
        entries = registry.entries()
        matching = [e for e in entries if e.namespace == "ns" and e.sub == "sub"]
        assert len(matching) == 1
        assert matching[0].help == "second"

    def test_namespace_and_sub_lowercased(self, registry):
        async def handler(args, context):
            pass

        registry.register("MEMORY", "CONSOLIDATE", handler)
        entries = registry.entries()
        assert entries[0].namespace == "memory"
        assert entries[0].sub == "consolidate"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    @pytest.mark.asyncio
    async def test_non_slash_text_not_handled(self, registry):
        assert await registry.dispatch("hello there", {}) is False

    @pytest.mark.asyncio
    async def test_empty_slash_not_handled(self, registry):
        assert await registry.dispatch("/", {}) is False

    @pytest.mark.asyncio
    async def test_unknown_command_not_handled(self, registry):
        assert await registry.dispatch("/nonexistent", {}) is False

    @pytest.mark.asyncio
    async def test_args_passed_through(self, registry):
        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("heartbeat", "run", handler)
        await registry.dispatch("/heartbeat run extra args here", {})
        assert seen == [["extra", "args", "here"]]

    @pytest.mark.asyncio
    async def test_context_passed_through(self, registry):
        seen = []

        async def handler(args, context):
            seen.append(context)

        registry.register("ns", "", handler)
        ctx = {"agent": "obj"}
        await registry.dispatch("/ns", ctx)
        assert seen == [ctx]

    @pytest.mark.asyncio
    async def test_unrecognised_sub_falls_back_to_bare_namespace_with_shifted_args(self, registry):
        """If /namespace word2 isn't a registered sub, and a bare /namespace
        handler exists, word2 shifts back into args."""
        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("memory", "", handler)
        await registry.dispatch("/memory some free text", {})
        assert seen == [["some", "free", "text"]]

    @pytest.mark.asyncio
    async def test_exception_in_handler_is_caught_and_reports_handled(self, registry):
        async def handler(args, context):
            raise RuntimeError("boom")

        registry.register("ns", "", handler)
        handled = await registry.dispatch("/ns", {})
        assert handled is True  # dispatch swallows handler exceptions


# ---------------------------------------------------------------------------
# Help listing / entries
# ---------------------------------------------------------------------------

class TestListing:
    def test_list_commands_sorted_and_formatted(self, registry):
        async def h(args, context):
            pass

        registry.register("zeta", "", h, help="zeta help")
        registry.register("alpha", "run", h, help="alpha help")
        rows = registry.list_commands()
        assert rows == [("/alpha run", "alpha help"), ("/zeta", "zeta help")]

    def test_entries_returns_copy(self, registry):
        async def h(args, context):
            pass

        registry.register("ns", "", h)
        entries = registry.entries()
        entries.append("bogus")
        assert len(registry.entries()) == 1

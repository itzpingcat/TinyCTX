"""
tests/test_commands.py

Tests for utils/commands.py — CommandRegistry, the slash-command dispatch
registry used by bridges before pushing text to the router.

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.permissions import Permission
from TinyCTX.utils.commands import CommandRegistry


@pytest.fixture
def registry():
    return CommandRegistry()


class _FakeCaller:
    def __init__(self, granted_permissions=None, username: str = "test-caller"):
        self._granted = (
            frozenset(granted_permissions) if granted_permissions is not None
            else frozenset(Permission)
        )
        self.username = username

    def effective_permissions(self, permissions_config=None) -> "frozenset[Permission]":
        return self._granted

    def has_permission(self, perm, permissions_config=None) -> bool:
        return perm in self._granted


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


# ---------------------------------------------------------------------------
# Permission gating (docs/PERMISSIONS-PLAN.md §9) — the CommandRegistry.dispatch
# seam, the second of the two seams the whole rework hangs off of (the other
# being ToolCallHandler.execute_tool_call, covered in test_tool_handler.py).
# ---------------------------------------------------------------------------

class TestPermissionGating:
    @pytest.mark.asyncio
    async def test_caller_with_required_permission_dispatches(self, registry):
        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("model", "", handler, required_permissions={Permission.MODEL_SWAP})
        caller = _FakeCaller(granted_permissions={Permission.MODEL_SWAP})
        handled = await registry.dispatch("/model", {"caller": caller})
        assert handled is True
        assert seen == [[]]

    @pytest.mark.asyncio
    async def test_caller_missing_required_permission_denied(self, registry):
        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("model", "", handler, required_permissions={Permission.MODEL_SWAP})
        caller = _FakeCaller(granted_permissions=set())
        handled = await registry.dispatch("/model", {"caller": caller})
        assert handled is True  # denial is still "handled" — not pushed to router
        assert seen == []  # handler never ran

    @pytest.mark.asyncio
    async def test_denial_message_sent_via_context_send(self, registry):
        async def handler(args, context):
            pass

        sent = []

        async def send(text):
            sent.append(text)

        registry.register("model", "", handler, required_permissions={Permission.MODEL_SWAP})
        caller = _FakeCaller(granted_permissions=set())
        await registry.dispatch("/model", {"caller": caller, "send": send})
        assert len(sent) == 1
        assert "PERMISSION DENIED" in sent[0]
        assert "model_swap" in sent[0]

    @pytest.mark.asyncio
    async def test_required_permissions_none_is_ungated(self, registry):
        """required_permissions=None must dispatch regardless of caller —
        including when no caller can be resolved at all."""
        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("help", "", handler, required_permissions=None)
        handled = await registry.dispatch("/help", {})
        assert handled is True
        assert seen == [[]]

    @pytest.mark.asyncio
    async def test_no_resolvable_caller_denied_for_gated_command(self, registry):
        """A gated command with no caller info in context at all must be
        denied, not treated as an unauthenticated free pass."""
        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("model", "", handler, required_permissions={Permission.MODEL_SWAP})
        handled = await registry.dispatch("/model", {})
        assert handled is True
        assert seen == []

    @pytest.mark.asyncio
    async def test_caller_resolved_via_platform_and_user_id(self, registry):
        """The alternate caller-resolution path: context supplies
        caller_platform + caller_user_id instead of a pre-resolved caller,
        and CommandRegistry._resolve_caller() looks it up via
        runtime.users.get_by_platform()."""
        from TinyCTX.contracts import Platform

        seen = []

        async def handler(args, context):
            seen.append(args)

        registry.register("model", "", handler, required_permissions={Permission.MODEL_SWAP})

        resolved_caller = _FakeCaller(granted_permissions={Permission.MODEL_SWAP})

        class _FakeUsers:
            def get_by_platform(self, platform, user_id):
                assert platform == Platform.DISCORD
                assert user_id == "12345"
                return resolved_caller

        class _FakeRuntime:
            users = _FakeUsers()

        handled = await registry.dispatch("/model", {
            "runtime": _FakeRuntime(),
            "caller_platform": "discord",
            "caller_user_id": "12345",
        })
        assert handled is True
        assert seen == [[]]

    @pytest.mark.asyncio
    async def test_multiple_required_permissions_all_must_be_held(self, registry):
        async def handler(args, context):
            pass

        registry.register(
            "admin", "", handler,
            required_permissions={Permission.ROOT, Permission.USER_READ},
        )
        partial = _FakeCaller(granted_permissions={Permission.ROOT})
        handled = await registry.dispatch("/admin", {"caller": partial})
        assert handled is True  # handled-as-denied

        full = _FakeCaller(granted_permissions={Permission.ROOT, Permission.USER_READ})
        seen = []

        async def handler2(args, context):
            seen.append(True)

        registry.register(
            "admin2", "", handler2,
            required_permissions={Permission.ROOT, Permission.USER_READ},
        )
        await registry.dispatch("/admin2", {"caller": full})
        assert seen == [True]

    @pytest.mark.asyncio
    async def test_network_write_implies_network_read_requirement(self, registry):
        """expand() applies to the requirement here too — a command
        declaring NETWORK_WRITE demands both bools (§1.2, mirrored at the
        command seam)."""
        seen = []

        async def handler(args, context):
            seen.append(True)

        registry.register("push", "", handler, required_permissions={Permission.NETWORK_WRITE})
        write_only = _FakeCaller(granted_permissions={Permission.NETWORK_WRITE})
        handled = await registry.dispatch("/push", {"caller": write_only})
        assert handled is True
        assert seen == []  # denied — missing the implied NETWORK_READ

        both = _FakeCaller(granted_permissions={Permission.NETWORK_WRITE, Permission.NETWORK_READ})
        await registry.dispatch("/push", {"caller": both})
        assert seen == [True]


# ---------------------------------------------------------------------------
# assert_permissions_declared() (§9, mirroring tool_handling.handler's)
# ---------------------------------------------------------------------------

class TestAssertPermissionsDeclared:
    def test_forgotten_declaration_trips_assertion(self, registry):
        async def handler(args, context):
            pass

        registry.register("ns", "", handler)  # forgot required_permissions
        with pytest.raises(RuntimeError) as excinfo:
            registry.assert_permissions_declared()
        assert "/ns" in str(excinfo.value)

    def test_explicit_none_does_not_trip_assertion(self, registry):
        async def handler(args, context):
            pass

        registry.register("ns", "", handler, required_permissions=None)
        registry.assert_permissions_declared()  # must not raise

    def test_explicit_set_does_not_trip_assertion(self, registry):
        async def handler(args, context):
            pass

        registry.register("ns", "", handler, required_permissions={Permission.ROOT})
        registry.assert_permissions_declared()  # must not raise

    def test_assertion_names_every_undeclared_command(self, registry):
        async def handler(args, context):
            pass

        registry.register("a", "", handler)
        registry.register("b", "sub", handler)
        with pytest.raises(RuntimeError) as excinfo:
            registry.assert_permissions_declared()
        msg = str(excinfo.value)
        assert "/a" in msg
        assert "/b sub" in msg

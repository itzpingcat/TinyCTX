"""
tests/test_decorators.py — unit tests for TinyCTX/decorators.py
(@tool, @hook, @command, @prompt) and walk_bindings(). See
docs/MODULES-PLAN-P1.md's "The module interface" section.

These test the decorators/walking mechanism in isolation — not yet wired
into module_registry.py's loader or any real module (that's a separate,
later step).
"""
from __future__ import annotations

import pytest

from TinyCTX.decorators import (
    CommandBinding,
    HookBinding,
    PromptBinding,
    ToolBinding,
    bindings_of,
    command,
    hook,
    prompt,
    tool,
    walk_bindings,
)
from TinyCTX.hooks import HookType
from TinyCTX.module import Module


class TestToolDecorator:
    def test_requires_permissions_kwarg(self):
        with pytest.raises(TypeError):
            tool()  # missing required permissions=

    def test_permissions_none_is_explicitly_allowed(self):
        @tool(permissions=None)
        def fn():
            pass

        binding = bindings_of(fn)[0]
        assert isinstance(binding, ToolBinding)
        assert binding.permissions is None

    def test_permissions_set_is_recorded(self):
        perms = {"file_write"}

        @tool(permissions=perms)
        def fn():
            pass

        assert bindings_of(fn)[0].permissions is perms

    def test_callable_permissions_classifier_is_recorded_unchanged(self):
        def classifier(**kwargs):
            return set()

        @tool(permissions=classifier)
        def fn():
            pass

        assert bindings_of(fn)[0].permissions is classifier

    def test_default_timeout_is_600(self):
        @tool(permissions=None)
        def fn():
            pass

        assert bindings_of(fn)[0].timeout == 600.0

    def test_timeout_override(self):
        @tool(permissions=None, timeout=120)
        def fn():
            pass

        assert bindings_of(fn)[0].timeout == 120

    def test_always_on_and_name_pass_through(self):
        @tool(permissions=None, always_on=True, name="custom_name")
        def fn():
            pass

        b = bindings_of(fn)[0]
        assert b.always_on is True
        assert b.name == "custom_name"


class TestHookDecorator:
    def test_requires_hooktype_member(self):
        with pytest.raises(TypeError):
            hook("transform_turn")  # a string, not HookType

    def test_records_type_and_priority(self):
        @hook(HookType.TRANSFORM_TURN, priority=5)
        def fn():
            pass

        b = bindings_of(fn)[0]
        assert isinstance(b, HookBinding)
        assert b.type is HookType.TRANSFORM_TURN
        assert b.priority == 5

    def test_default_priority_is_zero(self):
        @hook(HookType.PRE_ASSEMBLE)
        def fn():
            pass

        assert bindings_of(fn)[0].priority == 0

    def test_multiple_hook_decorations_on_different_methods_are_independent(self):
        @hook(HookType.FILTER_TURN, priority=0)
        def a():
            pass

        @hook(HookType.TRANSFORM_TURN, priority=1)
        def b():
            pass

        assert bindings_of(a)[0].type is HookType.FILTER_TURN
        assert bindings_of(b)[0].type is HookType.TRANSFORM_TURN


class TestCommandDecorator:
    def test_requires_permissions_kwarg(self):
        with pytest.raises(TypeError):
            command("notes", "list")  # missing required permissions=

    def test_permissions_none_is_explicitly_allowed(self):
        @command("notes", "list", permissions=None)
        def fn():
            pass

        b = bindings_of(fn)[0]
        assert isinstance(b, CommandBinding)
        assert b.permissions is None

    def test_namespace_sub_help_params_recorded(self):
        @command("notes", "list", permissions=None, help="List notes",
                 params=[("category", str, "which category")])
        def fn():
            pass

        b = bindings_of(fn)[0]
        assert b.namespace == "notes"
        assert b.sub == "list"
        assert b.help == "List notes"
        assert b.params == [("category", str, "which category")]

    def test_sub_defaults_to_empty_string_for_bare_namespace_command(self):
        @command("memory", permissions={"root"})
        def fn():
            pass

        assert bindings_of(fn)[0].sub == ""


class TestPromptDecorator:
    def test_defaults(self):
        @prompt()
        def fn():
            pass

        b = bindings_of(fn)[0]
        assert isinstance(b, PromptBinding)
        assert b.role == "system"
        assert b.priority == 0
        assert b.name is None

    def test_role_priority_name_override(self):
        @prompt(role="user", priority=10, name="footer")
        def fn():
            pass

        b = bindings_of(fn)[0]
        assert b.role == "user"
        assert b.priority == 10
        assert b.name == "footer"


class TestWalkBindings:
    def test_finds_all_decorated_methods_on_a_module_class(self):
        class Notes(Module):
            @hook(HookType.STARTUP)
            def open_store(self, runtime):
                pass

            @tool(permissions=None)
            def note_create(self, name, content):
                pass

            @command("notes", "list", permissions=None)
            def cmd_list(self, args, context):
                pass

            @prompt(role="user", priority=5)
            def footer(self, ctx):
                pass

            def _slug(self, name):  # untagged — must NOT appear
                return name.lower()

        instance = Notes()
        results = walk_bindings(instance)
        names = {name for name, _binding, _bound in results}
        assert names == {"open_store", "note_create", "cmd_list", "footer"}
        assert "_slug" not in names

    def test_bound_methods_are_callable_against_the_instance(self):
        class Counter(Module):
            def __init__(self):
                self.count = 0

            @tool(permissions=None)
            def increment(self):
                self.count += 1
                return self.count

        instance = Counter()
        [(_name, _binding, bound)] = walk_bindings(instance)
        assert bound() == 1
        assert instance.count == 1

    def test_untagged_module_has_no_bindings(self):
        class Empty(Module):
            def helper(self):
                pass

        assert walk_bindings(Empty()) == []

    def test_same_type_different_priority_tagging_works(bself):
        """MODULES-PLAN-P1.md: multiple methods on one class may tag the
        same HookType at different priorities — required for ctx_tools'
        three transform_turn handlers at priorities 0, 5, 10."""

        class CtxTools(Module):
            @hook(HookType.TRANSFORM_TURN, priority=0)
            def dedup(self, entry, age, ctx):
                pass

            @hook(HookType.TRANSFORM_TURN, priority=5)
            def cot_strip(self, entry, age, ctx):
                pass

            @hook(HookType.TRANSFORM_TURN, priority=10)
            def trim(self, entry, age, ctx):
                pass

        results = walk_bindings(CtxTools())
        priorities = sorted(b.priority for _n, b, _f in results)
        assert priorities == [0, 5, 10]
        assert all(b.type is HookType.TRANSFORM_TURN for _n, b, _f in results)

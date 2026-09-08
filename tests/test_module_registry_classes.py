"""Unit tests for module_registry.py's Module-class loading path."""
from __future__ import annotations

import types

import pytest

from TinyCTX.decorators import command, hook, prompt, tool
from TinyCTX.hooks import HookType
from TinyCTX.module import Module
from TinyCTX.module_registry import ModuleRegistry


def _make_module_object(module_class, module_name="fake_mod"):
    mod = types.ModuleType(module_name)
    module_class.__module__ = module_name
    mod.SomeModule = module_class
    return mod


class _FakeToolHandler:
    def __init__(self):
        self.registered = []

    def register_tool(self, func, name=None, always_on=False,
                       required_permissions=None, listing_permissions=None):
        self.registered.append((func, name, always_on, required_permissions, listing_permissions))


class _FakeContext:
    def __init__(self):
        self.hooks = []
        self.prompts = []

    def register_hook(self, stage, fn, *, priority=0):
        self.hooks.append((stage, fn, priority))

    def register_prompt(self, pid, provider, *, role="system", priority=0):
        self.prompts.append((pid, provider, role, priority))


class _FakeCycle:
    def __init__(self):
        self.tool_handler = _FakeToolHandler()
        self.context = _FakeContext()


class _FakeCommands:
    def __init__(self):
        self.registered = []

    def register(self, namespace, sub, handler, *, help="", params=None, required_permissions=None):
        self.registered.append((namespace, sub, handler, help, params, required_permissions))


class _FakeRuntime:
    def __init__(self):
        self.commands = _FakeCommands()


class TestFindModuleClass:
    def test_finds_module_subclass_defined_in_the_module(self):
        class MyMod(Module):
            pass

        mod = _make_module_object(MyMod)
        found = ModuleRegistry._find_module_class(mod)
        assert found is MyMod

    def test_ignores_bare_module_import(self):
        mod = types.ModuleType("fake_mod")
        mod.Module = Module  # merely imported, not defined here
        found = ModuleRegistry._find_module_class(mod)
        assert found is None

    def test_returns_none_for_function_based_module(self):
        mod = types.ModuleType("fake_mod")

        def register_agent(cycle):
            pass

        mod.register_agent = register_agent
        found = ModuleRegistry._find_module_class(mod)
        assert found is None


class TestRegisterModuleClass:
    def test_loads_and_wires_tool_hook_prompt(self):
        class Notes(Module):
            @tool(permissions=None)
            def note_create(self, name, content):
                pass

            @hook(HookType.TRANSFORM_TURN, priority=5)
            def trim(self, entry, age, ctx):
                pass

            @prompt(role="user")
            def footer(self, ctx):
                pass

        mod = _make_module_object(Notes)
        runtime = _FakeRuntime()
        registry = ModuleRegistry()
        registry._register_one(mod, runtime, "notes")

        cycle = _FakeCycle()
        registry.register_agent(cycle)

        assert len(cycle.tool_handler.registered) == 1
        assert cycle.tool_handler.registered[0][1] is None  # name not overridden

        assert len(cycle.context.hooks) == 1
        stage, fn, priority = cycle.context.hooks[0]
        assert stage == "transform_turn"
        assert priority == 5

        assert len(cycle.context.prompts) == 1
        pid, _fn, role, _priority = cycle.context.prompts[0]
        assert pid == "notes.footer"
        assert role == "user"

    def test_command_binding_registers_on_runtime_not_per_cycle(self):
        class Notes(Module):
            @command("notes", "list", permissions=None, help="List notes")
            def cmd_list(self, args, context):
                pass

        mod = _make_module_object(Notes)
        runtime = _FakeRuntime()
        registry = ModuleRegistry()
        registry._register_one(mod, runtime, "notes")

        assert len(runtime.commands.registered) == 1
        namespace, sub, _handler, help_text, _params, _perms = runtime.commands.registered[0]
        assert namespace == "notes"
        assert sub == "list"
        assert help_text == "List notes"

        # Command wiring must not also happen per-cycle.
        cycle = _FakeCycle()
        registry.register_agent(cycle)
        assert cycle.tool_handler.registered == []

    def test_missing_dependency_skips_module_entirely(self):
        class Broken(Module):
            dependencies = ("definitely_not_a_real_package_xyz123",)

            @tool(permissions=None)
            def whatever(self):
                pass

        mod = _make_module_object(Broken)
        runtime = _FakeRuntime()
        registry = ModuleRegistry()
        registry._register_one(mod, runtime, "broken")

        cycle = _FakeCycle()
        registry.register_agent(cycle)
        assert cycle.tool_handler.registered == []
        assert runtime.commands.registered == []

    def test_settings_resolved_onto_instance_config(self):
        class Notes(Module):
            settings = {"max_note_chars": {"default": 8000}}

            @tool(permissions=None)
            def whatever(self):
                pass

        mod = _make_module_object(Notes)
        runtime = _FakeRuntime()
        runtime.config = types.SimpleNamespace(extra={"notes": {"max_note_chars": 500}})
        registry = ModuleRegistry()
        registry._register_one(mod, runtime, "notes")

        assert registry._module_instances[0].config == {"max_note_chars": 500}

    def test_function_based_and_class_based_modules_coexist(self):
        calls = []

        def register_agent(cycle):
            calls.append("function_based")

        func_mod = types.ModuleType("func_mod")
        func_mod.register_agent = register_agent

        class ClassMod(Module):
            @tool(permissions=None)
            def whatever(self):
                pass

        class_mod = _make_module_object(ClassMod, module_name="class_mod")

        runtime = _FakeRuntime()
        registry = ModuleRegistry()
        registry._register_one(func_mod, runtime, "func_mod")
        registry._register_one(class_mod, runtime, "class_mod")

        cycle = _FakeCycle()
        registry.register_agent(cycle)

        assert calls == ["function_based"]
        assert len(cycle.tool_handler.registered) == 1

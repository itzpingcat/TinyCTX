"""
tests/test_module_registry.py

Tests for module_registry.py — ModuleRegistry, which scans modules/ and
custom_modules/ directories and instantiates each Module subclass it finds
(MODULES-PLAN-P1.md — the legacy register_runtime/register_agent
function-pair path is gone as of P3; every module in this repo is
Module-class-based now).

These tests exercise the discoverable/pure parts only: directory scanning,
path-based loading (custom_modules style, no import machinery needed),
skipping invalid entries, and register_agent fan-out over loaded module
instances. Loading via the "TinyCTX.modules.<name>" import_prefix path is
not covered here — that requires modules to be real importable packages
under TinyCTX/modules and is exercised indirectly by test_skills.py
already. A full AgentCycle/Runtime integration is out of scope for this
pass. Binding-walking mechanics (_register_module_class/_wire_module_instance
themselves) are covered by test_module_registry_classes.py; this file is
about *finding* a module on disk, not what happens once one is found.

Run with:
    pytest tests/
"""
from __future__ import annotations

from TinyCTX.module_registry import ModuleRegistry


def _write_module(dir_path, name, body):
    """Write a Module-class-based fixture module to disk. `body` is
    arbitrary source appended after the standard Module/decorator imports —
    give it a `class <Something>(Module): ...` to make it discoverable."""
    mod_dir = dir_path / name
    mod_dir.mkdir(parents=True)
    preamble = (
        "from TinyCTX.decorators import hook\n"
        "from TinyCTX.hooks import HookType\n"
        "from TinyCTX.module import Module\n\n"
    )
    (mod_dir / "__init__.py").write_text(preamble + body, encoding="utf-8")
    return mod_dir


class _FakeRuntime:
    pass


class _FakeToolHandler:
    def register_tool(self, *a, **k):
        pass


class _FakeContext:
    def register_hook(self, *a, **k):
        pass

    def register_prompt(self, *a, **k):
        pass


class _FakeCycle:
    def __init__(self):
        self.tool_handler = _FakeToolHandler()
        self.context = _FakeContext()


class TestLoadFromDirPathBased:
    """Exercises _load_from_dir with import_prefix=None — the custom_modules
    code path, which loads via importlib.util.spec_from_file_location and
    doesn't require the module to be an importable package."""

    def test_skips_missing_directory(self, tmp_path):
        registry = ModuleRegistry()
        missing = tmp_path / "does_not_exist"
        registry._load_from_dir(missing, _FakeRuntime(), import_prefix=None)
        assert registry._module_instances == []

    def test_skips_dir_without_init_or_main(self, tmp_path):
        mod_dir = tmp_path / "not_a_module"
        mod_dir.mkdir()
        (mod_dir / "readme.txt").write_text("nothing here", encoding="utf-8")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert registry._module_instances == []

    def test_loads_module_with_startup_hook(self, tmp_path):
        _write_module(tmp_path, "mod_a",
                      "class ModA(Module):\n"
                      "    @hook(HookType.STARTUP)\n"
                      "    def load(self, runtime):\n"
                      "        runtime.touched = True\n")
        registry = ModuleRegistry()
        runtime = _FakeRuntime()
        registry._load_from_dir(tmp_path, runtime, import_prefix=None)
        assert len(registry._module_instances) == 1
        assert runtime.touched is True

    def test_loads_module_with_only_tool_start_hook(self, tmp_path):
        _write_module(tmp_path, "mod_b",
                      "class ModB(Module):\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        cycle.touched = True\n")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert len(registry._module_instances) == 1
        cycle = _FakeCycle()
        registry.register_agent(cycle)
        assert cycle.touched is True

    def test_startup_runs_and_turn_start_wires_on_register_agent(self, tmp_path):
        _write_module(tmp_path, "mod_c",
                      "class ModC(Module):\n"
                      "    @hook(HookType.STARTUP)\n"
                      "    def load(self, runtime):\n"
                      "        self.loaded = True\n\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        cycle.wired = self.loaded\n")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert len(registry._module_instances) == 1
        cycle = _FakeCycle()
        registry.register_agent(cycle)
        assert cycle.wired is True

    def test_module_raising_in_load_is_skipped_not_fatal(self, tmp_path):
        _write_module(tmp_path, "mod_broken", "raise RuntimeError('boom at import time')\n")
        _write_module(tmp_path, "mod_ok",
                      "class ModOk(Module):\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        pass\n")
        registry = ModuleRegistry()
        # should not raise despite mod_broken failing
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert len(registry._module_instances) == 1

    def test_module_with_no_module_class_is_skipped(self, tmp_path):
        _write_module(tmp_path, "mod_empty", "x = 1\n")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert registry._module_instances == []

    def test_module_with_missing_dependency_is_skipped(self, tmp_path):
        _write_module(tmp_path, "mod_needs_dep",
                      "class ModNeedsDep(Module):\n"
                      "    dependencies = ('no_such_package_xyz',)\n")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert registry._module_instances == []


class TestRegisterAgent:
    def test_register_agent_wires_every_loaded_instance(self, tmp_path):
        registry = ModuleRegistry()
        _write_module(tmp_path, "mod_a",
                      "class ModA(Module):\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        cycle.a = True\n")
        _write_module(tmp_path, "mod_b",
                      "class ModB(Module):\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        cycle.b = True\n")
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        cycle = _FakeCycle()
        registry.register_agent(cycle)
        assert cycle.a is True
        assert cycle.b is True

    def test_register_agent_continues_after_one_instance_raises(self, tmp_path):
        registry = ModuleRegistry()
        _write_module(tmp_path, "mod_bad",
                      "class ModBad(Module):\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        raise RuntimeError('boom')\n")
        _write_module(tmp_path, "mod_good",
                      "class ModGood(Module):\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        cycle.good = True\n")
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        cycle = _FakeCycle()
        registry.register_agent(cycle)  # should not raise
        assert cycle.good is True

    def test_register_agent_with_no_modules_is_noop(self):
        registry = ModuleRegistry()
        registry.register_agent(_FakeCycle())  # should not raise


class TestLoadModules:
    def test_load_modules_scans_both_dirs(self, tmp_path, monkeypatch):
        import TinyCTX.module_registry as module_registry_mod

        modules_dir = tmp_path / "modules"
        custom_dir = tmp_path / "custom_modules"
        modules_dir.mkdir()
        custom_dir.mkdir()
        _write_module(custom_dir, "custom_one",
                      "class CustomOne(Module):\n"
                      "    @hook(HookType.TURN_START)\n"
                      "    def wire(self, cycle):\n"
                      "        pass\n")

        monkeypatch.setattr(module_registry_mod, "MODULES_DIR", modules_dir)
        monkeypatch.setattr(module_registry_mod, "CUSTOM_MODULES_DIR", custom_dir)

        registry = ModuleRegistry()
        registry.load_modules(_FakeRuntime())
        assert len(registry._module_instances) == 1

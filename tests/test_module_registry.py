"""
tests/test_module_registry.py

Tests for module_registry.py — ModuleRegistry, which scans modules/ and
custom_modules/ directories and wires register_runtime/register_agent hooks.

These tests exercise the discoverable/pure parts only: directory scanning,
path-based loading (custom_modules style, no import machinery needed),
skipping invalid entries, and register_agent fan-out. Loading via the
"TinyCTX.modules.<name>" import_prefix path is not covered here — that
requires modules to be real importable packages under TinyCTX/modules and
is exercised indirectly by test_skills.py already. A full AgentCycle/Runtime
integration is out of scope for this pass.

Run with:
    pytest tests/
"""
from __future__ import annotations

from TinyCTX.module_registry import ModuleRegistry


def _write_module(dir_path, name, body):
    mod_dir = dir_path / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "__init__.py").write_text(body, encoding="utf-8")
    return mod_dir


class _FakeRuntime:
    pass


class _FakeCycle:
    pass


class TestLoadFromDirPathBased:
    """Exercises _load_from_dir with import_prefix=None — the custom_modules
    code path, which loads via importlib.util.spec_from_file_location and
    doesn't require the module to be an importable package."""

    def test_skips_missing_directory(self, tmp_path):
        registry = ModuleRegistry()
        missing = tmp_path / "does_not_exist"
        registry._load_from_dir(missing, _FakeRuntime(), import_prefix=None)
        assert registry._agent_registrations == []

    def test_skips_dir_without_init_or_main(self, tmp_path):
        mod_dir = tmp_path / "not_a_module"
        mod_dir.mkdir()
        (mod_dir / "readme.txt").write_text("nothing here", encoding="utf-8")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert registry._agent_registrations == []

    def test_loads_module_with_register_runtime_only(self, tmp_path):
        _write_module(tmp_path, "mod_a", "calls = []\n\n"
                      "def register_runtime(runtime):\n"
                      "    calls.append(runtime)\n")
        registry = ModuleRegistry()
        runtime = _FakeRuntime()
        registry._load_from_dir(tmp_path, runtime, import_prefix=None)
        # register_runtime is called immediately; no register_agent to queue
        assert registry._agent_registrations == []

    def test_loads_module_with_register_agent_only(self, tmp_path):
        _write_module(tmp_path, "mod_b", "def register_agent(cycle):\n"
                      "    cycle.touched = True\n")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert len(registry._agent_registrations) == 1

    def test_register_runtime_called_and_agent_hook_queued(self, tmp_path):
        _write_module(tmp_path, "mod_c",
                      "runtime_calls = []\n\n"
                      "def register_runtime(runtime):\n"
                      "    runtime_calls.append(1)\n\n"
                      "def register_agent(cycle):\n"
                      "    cycle.wired = True\n")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert len(registry._agent_registrations) == 1
        cycle = _FakeCycle()
        registry._agent_registrations[0](cycle)
        assert cycle.wired is True

    def test_module_raising_in_load_is_skipped_not_fatal(self, tmp_path):
        _write_module(tmp_path, "mod_broken", "raise RuntimeError('boom at import time')\n")
        _write_module(tmp_path, "mod_ok", "def register_agent(cycle):\n    pass\n")
        registry = ModuleRegistry()
        # should not raise despite mod_broken failing
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert len(registry._agent_registrations) == 1

    def test_module_with_neither_hook_is_skipped(self, tmp_path):
        _write_module(tmp_path, "mod_empty", "x = 1\n")
        registry = ModuleRegistry()
        registry._load_from_dir(tmp_path, _FakeRuntime(), import_prefix=None)
        assert registry._agent_registrations == []


class TestRegisterAgent:
    def test_register_agent_calls_all_queued_hooks(self):
        registry = ModuleRegistry()
        calls = []
        registry._agent_registrations.append(lambda cycle: calls.append("a"))
        registry._agent_registrations.append(lambda cycle: calls.append("b"))
        registry.register_agent(_FakeCycle())
        assert calls == ["a", "b"]

    def test_register_agent_continues_after_hook_raises(self):
        registry = ModuleRegistry()
        calls = []

        def bad(cycle):
            raise RuntimeError("boom")

        def good(cycle):
            calls.append("good")

        registry._agent_registrations.append(bad)
        registry._agent_registrations.append(good)
        registry.register_agent(_FakeCycle())  # should not raise
        assert calls == ["good"]

    def test_register_agent_with_no_hooks_is_noop(self):
        registry = ModuleRegistry()
        registry.register_agent(_FakeCycle())  # should not raise


class TestLoadModules:
    def test_load_modules_scans_both_dirs(self, tmp_path, monkeypatch):
        import TinyCTX.module_registry as module_registry_mod

        modules_dir = tmp_path / "modules"
        custom_dir = tmp_path / "custom_modules"
        modules_dir.mkdir()
        custom_dir.mkdir()
        _write_module(custom_dir, "custom_one", "def register_agent(cycle):\n    pass\n")

        monkeypatch.setattr(module_registry_mod, "MODULES_DIR", modules_dir)
        monkeypatch.setattr(module_registry_mod, "CUSTOM_MODULES_DIR", custom_dir)

        registry = ModuleRegistry()
        registry.load_modules(_FakeRuntime())
        assert len(registry._agent_registrations) == 1

"""
tests/test_equipment_manifest.py

Integration tests for modules/equipment_manifest.
The template engine is Jinja2 — no need to unit-test it.
We test: path resolution, register() behaviour, and that variables
are correctly threaded through to the rendered output.
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from TinyCTX.modules.equipment_manifest.__main__ import _resolve_em_path, register


# ---------------------------------------------------------------------------
# _resolve_em_path
# ---------------------------------------------------------------------------

class TestResolveEmPath:
    def test_empty_returns_module_em_md(self, tmp_path):
        module_dir = tmp_path / "module"
        module_dir.mkdir()
        result = _resolve_em_path("", module_dir, tmp_path / "workspace")
        assert result == module_dir / "EM.md"

    def test_workspace_prefix(self, tmp_path):
        result = _resolve_em_path("workspace:custom/EM.md", tmp_path / "module", tmp_path)
        assert result == (tmp_path / "custom/EM.md").resolve()

    def test_absolute_path(self, tmp_path):
        abs_path = str(tmp_path / "absolute.md")
        result = _resolve_em_path(abs_path, tmp_path / "module", tmp_path / "ws")
        assert result == Path(abs_path)

    def test_relative_resolves_under_workspace(self, tmp_path):
        result = _resolve_em_path("subdir/EM.md", tmp_path / "module", tmp_path)
        assert result == (tmp_path / "subdir/EM.md").resolve()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(workspace: Path, config_path: str = "", extra: dict | None = None) -> MagicMock:
    agent = MagicMock()
    agent.config.workspace.path = str(workspace)
    agent.config.config_path = config_path
    agent.config.extra = extra or {}
    agent.context.register_prompt = MagicMock()
    return agent


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

class TestRegister:
    def test_noop_when_em_md_missing(self, tmp_path):
        agent = _make_agent(tmp_path)
        # No EM.md anywhere — module dir default won't exist in tmp_path
        # Point em_path at a nonexistent file
        agent.config.extra = {"equipment_manifest": {"em_path": str(tmp_path / "nonexistent.md")}}
        register(agent)
        agent.context.register_prompt.assert_not_called()

    def test_noop_when_disabled(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("hello", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {
            "em_path": str(em), "enabled": False
        }})
        register(agent)
        agent.context.register_prompt.assert_not_called()

    def test_registers_prompt_when_em_md_present(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("hello", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        agent.context.register_prompt.assert_called_once()
        pid = agent.context.register_prompt.call_args[0][0]
        assert pid == "equipment_manifest"

    def test_custom_priority(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("x", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {
            "em_path": str(em), "prompt_priority": 99
        }})
        register(agent)
        kwargs = agent.context.register_prompt.call_args[1]
        assert kwargs["priority"] == 99

    def test_provider_renders_workspace_path(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("WS={{ workspace_path }}", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        result = provider(None)
        assert str(tmp_path.resolve()) in result

    def test_provider_returns_none_for_empty_template(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("   \n\n  ", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        assert provider(None) is None

    def test_provider_renders_date_and_time(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("{{ date }} {{ time }}", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        result = provider(None)
        # date is YYYY-MM-DD, time is HH:MM
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", result)

    def test_platform_conditional(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text(
            "{% if system == 'Windows' %}win{% else %}posix{% endif %}",
            encoding="utf-8",
        )
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        result = provider(None)
        expected = "win" if platform.system() == "Windows" else "posix"
        assert result == expected

    def test_workspace_prefix_path(self, tmp_path):
        em = tmp_path / "MY_EM.md"
        em.write_text("hello jinja2", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": "workspace:MY_EM.md"}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        assert provider(None) == "hello jinja2"

    def test_jinja2_for_loop(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text(
            "{% for i in range(3) %}{{ i }} {% endfor %}",
            encoding="utf-8",
        )
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        assert provider(None) == "0 1 2"

    def test_jinja2_filter(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("{{ system | upper }}", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        result = provider(None)
        assert result == platform.system().upper()

    def test_syntax_error_returns_none(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("{% if %}", encoding="utf-8")  # invalid Jinja2
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        assert provider(None) is None

    def test_provider_renders_source_root(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text("SR={{ source_root }}", encoding="utf-8")
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        result = provider(None)
        # source_root is cwd at test time — just check it's a non-empty absolute path
        from pathlib import Path
        assert result.startswith("SR=")
        assert Path(result[3:]).is_absolute()

    def test_source_root_conditional(self, tmp_path):
        em = tmp_path / "EM.md"
        em.write_text(
            "{%- if source_root != workspace_path %}different{%- else %}same{%- endif %}",
            encoding="utf-8",
        )
        agent = _make_agent(tmp_path, extra={"equipment_manifest": {"em_path": str(em)}})
        register(agent)
        provider = agent.context.register_prompt.call_args[0][1]
        result = provider(None)
        from pathlib import Path
        cwd = str(Path.cwd().resolve())
        ws  = str(tmp_path.resolve())
        assert result == ("different" if cwd != ws else "same")

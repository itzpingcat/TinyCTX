from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import yaml

from bridges.cli.__main__ import CLIBridge, _DimToolLineProcessor
from config import (
    BridgeConfig,
    Config,
    LLMRoutingConfig,
    LoggingConfig,
    ModelConfig,
    WorkspaceConfig,
)
from main import _startup_log_level


def _make_config(
    tmp_path: Path,
    *,
    logging_level: str = "INFO",
    cli_options: dict | None = None,
    extra: dict | None = None,
) -> Config:
    return Config(
        models={
            "main": ModelConfig(
                base_url="http://localhost:8080/v1",
                model="llama3",
                api_key_env="N/A",
            )
        },
        llm=LLMRoutingConfig(primary="main"),
        workspace=WorkspaceConfig(path=tmp_path),
        logging=LoggingConfig(level=logging_level),
        bridges={"cli": BridgeConfig(enabled=True, options=cli_options or {})},
        extra=extra or {},
    )


def test_startup_log_level_defaults_to_warning_for_cli(tmp_path):
    cfg = _make_config(tmp_path, logging_level="INFO")
    assert _startup_log_level(cfg) == logging.WARNING


def test_startup_log_level_can_keep_info_when_quiet_startup_disabled(tmp_path):
    cfg = _make_config(tmp_path, logging_level="INFO", cli_options={"quiet_startup": False})
    assert _startup_log_level(cfg) == logging.INFO


def test_cli_runtime_log_level_defaults_to_warning(tmp_path):
    cfg = _make_config(tmp_path, logging_level="INFO")
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    assert bridge._resolve_runtime_log_level() == logging.WARNING


def test_cli_runtime_log_level_can_inherit_global_level(tmp_path):
    cfg = _make_config(tmp_path, logging_level="INFO")
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={"log_level": "inherit"})
    assert bridge._resolve_runtime_log_level() == logging.INFO


def test_cli_startup_summary_is_compact_and_informative(tmp_path):
    cfg = _make_config(
        tmp_path,
        extra={
            "memory": {"embedding_model": "embed"},
            "heartbeat": {"every_minutes": 15},
            "mcp": {"servers": {"github": {"command": "uvx"}}},
        },
    )
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    summary = bridge._startup_summary(logging.WARNING).plain
    assert "workspace" in summary
    assert "model llama3" in summary
    assert "memory embed" in summary
    assert "heartbeat 15m" in summary
    assert "mcp 1" in summary
    assert "logs warning+" in summary


def test_cli_welcome_screen_uses_tinyctx_ascii_logo_and_shortcuts(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    welcome = bridge._compose_welcome_text(logging.WARNING, width=100)
    assert "████████╗██╗███╗   ██╗██╗   ██╗" in welcome
    assert "Agent Framework" in welcome
    assert "cwd " in welcome
    first_left_line = bridge._welcome_lines(logging.WARNING, width=60)[0]
    assert first_left_line.startswith(" ")
    wide_first_line = bridge._compose_welcome_text(logging.WARNING, width=140).splitlines()[0]
    assert len(wide_first_line) - len(wide_first_line.lstrip(" ")) > 20


def test_cli_footer_tracks_working_status(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._set_status("web_search")
    footer = bridge._footer_text()
    assert "working web_search" in footer
    assert "Enter send" not in footer


def test_cli_output_wraps_while_input_stays_single_line(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    with patch("bridges.cli.__main__.Application", return_value=SimpleNamespace()) as app_cls:
        app = bridge._build_application()
    assert bridge._output_area is not None
    assert bridge._input_area is not None
    assert bridge._output_area.wrap_lines is False
    assert bridge._output_area.control.focusable() is True
    assert bridge._output_area.control.focus_on_click() is True
    assert bridge._input_area.buffer.multiline() is False
    assert app_cls.called
    assert app is not None


def test_cli_style_uses_black_background_and_red_banner(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    style = bridge._style().style_rules
    assert ("output-area", "#d7d7d7 bg:#000000") in style
    assert ("input-area", "#f5f5f5 bg:#000000") in style
    assert ("banner", "bold #ff3b30 bg:#000000") in style
    assert ("tool-dim", "#7f7f7f bg:#000000") in style
    first_fragment = bridge._welcome_fragments()[0]
    assert first_fragment[0] == "class:banner"


def test_cli_tool_lines_are_compact(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    assert bridge._tool_call_line("web_search", {"query": "NHL scores today"}) == 'tool web_search NHL scores today'
    assert bridge._tool_call_line("browse_url", {"url": "https://example.com", "mode": "text"}) == "tool browse_url https://example.com"
    assert bridge._tool_result_line("web_search", "Search results for NHL scores today", False) == "ok web_search Search results for NHL scores today"


def test_cli_dims_tool_prefix_lines():
    processor = _DimToolLineProcessor()
    transformed = processor.apply_transformation(
        SimpleNamespace(fragments=[("", "tool web_search NHL scores today")])
    )
    assert transformed.fragments == [("class:tool-dim", "tool web_search NHL scores today")]


def test_cli_wraps_transcript_by_words(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    wrapped = bridge._wrap_text_line(
        "Based on the latest NHL standings, the Anaheim Ducks are first in the Pacific Division.",
        40,
    )
    assert "Pacific" in wrapped
    assert "Pacif\nic" not in wrapped


def test_settings_command_opens_menu(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    asyncio.run(bridge._handle_command("/settings"))
    assert bridge._settings_open() is True
    assert bridge._settings_menu()[0] == "Settings"
    assert bridge._footer_text() == "working settings"


def test_settings_navigation_enters_submenu_and_applies_choice(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._open_settings()
    bridge._move_settings(1)
    bridge._activate_settings_selection()
    assert bridge._settings_path[-1] == "behavior"
    bridge._move_settings(1)
    bridge._activate_settings_selection()
    assert bridge._settings_path[-1] == "log_level"
    bridge._move_settings(3)
    bridge._activate_settings_selection()
    assert bridge._settings_path[-1] == "behavior"
    assert bridge._options["log_level"] == "debug"


def test_settings_round_trips_menu_updates_runtime_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
models:
  main:
    base_url: http://localhost:8080/v1
    model: llama3
    api_key_env: N/A
llm:
  primary: main
max_tool_cycles: 20
bridges:
  cli:
    enabled: true
    options: {}
""".strip(),
        encoding="utf-8",
    )
    cfg = _make_config(tmp_path)
    cfg.max_tool_cycles = 20
    setattr(cfg, "_source_path", cfg_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._open_settings()
    bridge._move_settings(1)
    bridge._activate_settings_selection()
    assert bridge._settings_path[-1] == "behavior"
    bridge._activate_settings_selection()
    assert bridge._settings_path[-1] == "round_trips"
    bridge._move_settings(2)
    bridge._activate_settings_selection()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["max_tool_cycles"] == 30
    assert cfg.max_tool_cycles == 30


def test_settings_toggle_persists_cli_option(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
models:
  main:
    base_url: http://localhost:8080/v1
    model: llama3
    api_key_env: N/A
llm:
  primary: main
bridges:
  cli:
    enabled: true
    options:
      compact_tools: true
""".strip(),
        encoding="utf-8",
    )
    cfg = _make_config(tmp_path)
    setattr(cfg, "_source_path", cfg_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={"compact_tools": True})
    bridge._apply_cli_option("compact_tools", False)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["bridges"]["cli"]["options"]["compact_tools"] is False


def test_settings_root_contains_session_submenu(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._open_settings()
    lines = "".join(fragment[1] for fragment in bridge._settings_fragments())
    assert "Appearance" in lines
    assert "Behavior" in lines
    assert "Session" in lines


def test_settings_behavior_menu_shows_round_trips_value(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.max_tool_cycles = 20
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._settings_path = ["root", "behavior"]
    bridge._settings_selected = [0, 0]
    lines = "".join(fragment[1] for fragment in bridge._settings_fragments())
    assert "Agent round trips" in lines
    assert "20" in lines

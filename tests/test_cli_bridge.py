from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import yaml

from prompt_toolkit.document import Document

from bridges.cli.__main__ import (
    CLIBridge,
    _DimToolLineProcessor,
    _SlashCommandCompleter,
)
from config import (
    BridgeConfig,
    Config,
    LLMRoutingConfig,
    LoggingConfig,
    ModelConfig,
    WorkspaceConfig,
)
from db import ConversationDB
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


def test_slash_command_completer_matches_prefix():
    completions = list(
        _SlashCommandCompleter().get_completions(
            Document(text="/se", cursor_position=3),
            None,
        )
    )
    assert [completion.display_text for completion in completions] == ["/settings"]
    assert [completion.text for completion in completions] == ["ttings"]


def test_slash_command_completer_supports_multiword_command():
    completions = list(
        _SlashCommandCompleter().get_completions(
            Document(text="/debug h", cursor_position=8),
            None,
        )
    )
    assert [completion.display_text for completion in completions] == ["/debug heartbeat"]
    assert [completion.text for completion in completions] == ["eartbeat"]


def test_slash_command_completer_supports_copy_tool_command():
    completions = list(
        _SlashCommandCompleter().get_completions(
            Document(text="/copy last-t", cursor_position=12),
            None,
        )
    )
    assert [completion.display_text for completion in completions] == ["/copy last-tool"]
    assert [completion.text for completion in completions] == ["ool"]


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
    bridge._move_settings(2)
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
    bridge._move_settings(2)
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
    assert "Providers" in lines
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


def test_settings_providers_menu_shows_active_profile_context(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._settings_path = ["root", "providers"]
    bridge._settings_selected = [0, 0]
    lines = "".join(fragment[1] for fragment in bridge._settings_fragments())
    assert "active profile main" in lines
    assert "Manage active profile" in lines


def test_settings_can_create_provider_preset_profile(tmp_path):
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
    options: {}
""".strip(),
        encoding="utf-8",
    )
    cfg = _make_config(tmp_path)
    cfg.models["ollama"] = ModelConfig(
        base_url="http://localhost:11434/v1",
        model="llama3.1",
        api_key_env="N/A",
    )
    setattr(cfg, "_source_path", cfg_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._settings_path = ["root", "providers", "providers_add"]
    bridge._settings_selected = [0, 0, 0]
    bridge._activate_settings_selection()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"]["openai"]["base_url"] == "https://api.openai.com/v1"
    assert cfg.models["openai"].base_url == "https://api.openai.com/v1"
    assert bridge._settings_path[-1] == "provider_profile:openai"


def test_settings_can_set_primary_profile_from_provider_menu(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
models:
  main:
    base_url: http://localhost:8080/v1
    model: llama3
    api_key_env: N/A
  ollama:
    base_url: http://localhost:11434/v1
    model: llama3.1
    api_key_env: N/A
llm:
  primary: main
bridges:
  cli:
    enabled: true
    options: {}
""".strip(),
        encoding="utf-8",
    )
    cfg = _make_config(tmp_path)
    cfg.models["ollama"] = ModelConfig(
        base_url="http://localhost:11434/v1",
        model="llama3.1",
        api_key_env="N/A",
    )
    setattr(cfg, "_source_path", cfg_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._settings_path = ["root", "providers", "providers_primary"]
    bridge._settings_selected = [0, 0, 1]
    bridge._activate_settings_selection()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["llm"]["primary"] == "ollama"
    assert cfg.llm.primary == "ollama"


def test_tab_completion_fills_unique_slash_command(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    with patch("bridges.cli.__main__.Application", return_value=SimpleNamespace()):
        bridge._build_application()
    assert bridge._input_area is not None
    assert isinstance(bridge._input_area.completer, _SlashCommandCompleter)
    bridge._input_area.buffer.document = Document(text="/set", cursor_position=4)
    bridge._complete_input()
    assert bridge._input_area.text == "/settings"


def test_cli_capture_root_logs_routes_warnings_to_transcript(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level

    with bridge._capture_root_logs(logging.INFO):
        logging.info("hidden info")
        logging.warning("keep warning")

    assert "warn keep warning" in bridge._transcript_blocks
    assert all("hidden info" not in block for block in bridge._transcript_blocks)
    assert root.handlers == previous_handlers
    assert root.level == previous_level


def test_large_paste_collapses_to_reference_and_expands_for_submit(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    with patch("bridges.cli.__main__.Application", return_value=SimpleNamespace()):
        bridge._build_application()
    assert bridge._input_area is not None

    pasted = "line one\nline two\nline three"
    bridge._handle_paste(pasted)

    assert bridge._input_area.text == "[Pasted text #1, 28 chars]"
    assert bridge._expand_pasted_text_refs(bridge._input_area.text) == pasted


def test_short_paste_also_uses_reference(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    with patch("bridges.cli.__main__.Application", return_value=SimpleNamespace()):
        bridge._build_application()

    bridge._handle_paste("hello")
    assert bridge._input_area is not None
    assert bridge._input_area.text == "[Pasted text #1, 5 chars]"
    assert bridge._expand_pasted_text_refs(bridge._input_area.text) == "hello"


def test_submit_text_renders_placeholder_but_sends_expanded_paste(tmp_path):
    cfg = _make_config(tmp_path)

    pushed = {}

    async def _push(msg):
        pushed["text"] = msg.text
        bridge._reply_done.set()

    gateway = SimpleNamespace(_config=cfg, push=_push)
    bridge = CLIBridge(gateway, options={})
    bridge._cursor = "cursor-1"

    asyncio.run(
        bridge._submit_text(
            "[Pasted text #1, 10 chars]",
            pasted_texts={1: "alpha\nbeta"},
        )
    )

    assert pushed["text"] == "alpha\nbeta"
    assert bridge._transcript_blocks[-1] == "› [Pasted text #1, 10 chars]"


def test_copy_primary_text_prefers_selected_output(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    with patch("bridges.cli.__main__.Application", return_value=SimpleNamespace()):
        bridge._build_application()

    assert bridge._output_area is not None
    bridge._output_area.buffer.selection_state = object()
    with patch.object(bridge._output_area.buffer, "copy_selection", return_value=SimpleNamespace(text="picked output")):
        with patch.object(bridge, "_write_clipboard_text", return_value=True) as copy_mock:
            assert bridge._copy_primary_text() is True

    copy_mock.assert_called_once_with("picked output")


def test_copy_primary_text_falls_back_to_full_transcript(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    with patch("bridges.cli.__main__.Application", return_value=SimpleNamespace()):
        bridge._build_application()

    bridge._transcript_blocks = ["line one", "line two"]
    bridge._refresh_output(logging.WARNING)

    with patch.object(bridge, "_write_clipboard_text", return_value=True) as copy_mock:
        assert bridge._copy_primary_text() is True

    copied_text = copy_mock.call_args[0][0]
    assert "line one" in copied_text
    assert "line two" in copied_text


def test_copy_command_copies_last_tool_block(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._transcript_blocks = [
        "› hello",
        "tool shell Get-ChildItem -Path C:\\repo",
        "err shell [stderr] missing path [exit 1]",
    ]
    with patch.object(bridge, "_write_clipboard_text", return_value=True) as copy_mock:
        asyncio.run(bridge._handle_command("/copy last-tool"))

    copy_mock.assert_called_once_with(
        "tool shell Get-ChildItem -Path C:\\repo\nerr shell [stderr] missing path [exit 1]"
    )
    assert bridge._transcript_blocks[-1] == "copied last tool block"


def test_copy_command_copies_last_error_block(tmp_path):
    cfg = _make_config(tmp_path)
    bridge = CLIBridge(SimpleNamespace(_config=cfg), options={})
    bridge._transcript_blocks = [
        "tool shell bad command",
        "err shell [stderr] boom [exit 1]",
        "› next",
    ]
    with patch.object(bridge, "_write_clipboard_text", return_value=True) as copy_mock:
        asyncio.run(bridge._handle_command("/copy last-error"))

    copy_mock.assert_called_once_with(
        "tool shell bad command\nerr shell [stderr] boom [exit 1]"
    )
    assert bridge._transcript_blocks[-1] == "copied last error block"


def test_cli_restores_transcript_from_saved_cursor(tmp_path):
    cfg = _make_config(tmp_path)
    gateway = SimpleNamespace(_config=cfg)
    bridge = CLIBridge(gateway, options={})

    db = ConversationDB(tmp_path / "agent.db")
    root = db.get_root()
    session = db.add_node(parent_id=root.id, role="system", content="session:cli")
    user = db.add_node(parent_id=session.id, role="user", content="hello")
    assistant = db.add_node(parent_id=user.id, role="assistant", content="hi there")
    bridge._cursor = assistant.id
    db.close()

    restored = bridge._restore_transcript_from_cursor()

    assert restored == 2
    assert bridge._transcript_blocks == ["› hello", "hi there"]


def test_cli_restores_tool_history_from_saved_cursor(tmp_path):
    cfg = _make_config(tmp_path)
    gateway = SimpleNamespace(_config=cfg)
    bridge = CLIBridge(gateway, options={})

    db = ConversationDB(tmp_path / "agent.db")
    root = db.get_root()
    session = db.add_node(parent_id=root.id, role="system", content="session:cli")
    user = db.add_node(parent_id=session.id, role="user", content="check cwd")
    assistant = db.add_node(
        parent_id=user.id,
        role="assistant",
        content="",
        tool_calls=json.dumps([
            {"id": "call-1", "name": "shell", "arguments": {"command": "pwd"}}
        ]),
    )
    tool = db.add_node(
        parent_id=assistant.id,
        role="tool",
        content="C:\\repo",
        tool_call_id="call-1",
    )
    bridge._cursor = tool.id
    db.close()

    restored = bridge._restore_transcript_from_cursor()

    assert restored == 3
    assert bridge._transcript_blocks[0] == "› check cwd"
    assert bridge._transcript_blocks[1] == "tool shell pwd"
    assert bridge._transcript_blocks[2] == "ok shell C:\\repo"


def test_cli_restores_shell_error_history_as_err(tmp_path):
    cfg = _make_config(tmp_path)
    gateway = SimpleNamespace(_config=cfg)
    bridge = CLIBridge(gateway, options={})

    db = ConversationDB(tmp_path / "agent.db")
    root = db.get_root()
    session = db.add_node(parent_id=root.id, role="system", content="session:cli")
    user = db.add_node(parent_id=session.id, role="user", content="check missing path")
    assistant = db.add_node(
        parent_id=user.id,
        role="assistant",
        content="",
        tool_calls=json.dumps([
            {"id": "call-1", "name": "shell", "arguments": {"command": "Get-ChildItem -Path C:\\missing"}}
        ]),
    )
    tool = db.add_node(
        parent_id=assistant.id,
        role="tool",
        content="[stderr]\nGet-ChildItem : Cannot find path\n[exit 1]",
        tool_call_id="call-1",
    )
    bridge._cursor = tool.id
    db.close()

    restored = bridge._restore_transcript_from_cursor()

    assert restored == 3
    assert bridge._transcript_blocks[2].startswith("err shell")

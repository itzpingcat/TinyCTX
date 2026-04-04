"""
bridges/cli/__main__.py — Fullscreen interactive CLI bridge using prompt_toolkit.
"""
from __future__ import annotations

import asyncio
import logging
import re
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from rich.text import Text

from config import (
    load as load_config,
    resolve_log_level,
    set_primary_model,
    update_bridge_options,
    update_config_values,
    update_model_profile,
)
from contracts import (
    AgentError,
    AgentTextChunk,
    AgentTextFinal,
    AgentThinkingChunk,
    AgentToolCall,
    AgentToolResult,
    ContentType,
    InboundMessage,
    Platform,
    UserIdentity,
)

logger = logging.getLogger(__name__)

_RICH_TAG = re.compile(r"\[[^\]]+\]")
_TINYCTX_BANNER = (
    "████████╗██╗███╗   ██╗██╗   ██╗ ██████╗████████╗██╗  ██╗",
    "╚══██╔══╝██║████╗  ██║╚██╗ ██╔╝██╔════╝╚══██╔══╝╚██╗██╔╝",
    "   ██║   ██║██╔██╗ ██║ ╚████╔╝ ██║        ██║    ╚███╔╝ ",
    "   ██║   ██║██║╚██╗██║  ╚██╔╝  ██║        ██║    ██╔██╗ ",
    "   ██║   ██║██║ ╚████║   ██║   ╚██████╗   ██║   ██╔╝ ██╗",
    "   ╚═╝   ╚═╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝   ╚═╝   ╚═╝  ╚═╝",
)
_DIMMED_LINE_PREFIXES = ("tool ", "ok ", "err ", "thinking…")
_CLI_OPTION_DEFAULTS = {
    "compact_tools": True,
    "dim_tools": True,
    "word_wrap": True,
    "quiet_startup": True,
}
_LOG_LEVEL_CHOICES = ("inherit", "warning", "info", "debug", "error")
_ROUND_TRIP_CHOICES = (10, 20, 30, 40, 60)
_PROVIDER_PRESETS = {
    "openai": {
        "label": "OpenAI",
        "profile": "openai",
        "updates": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key_env": "OPENAI_API_KEY",
        },
    },
    "openrouter": {
        "label": "OpenRouter",
        "profile": "openrouter",
        "updates": {
            "base_url": "https://openrouter.ai/api/v1",
            "model": "openai/gpt-4o-mini",
            "api_key_env": "OPENROUTER_API_KEY",
        },
    },
    "ollama": {
        "label": "Ollama",
        "profile": "ollama",
        "updates": {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3.1",
            "api_key_env": "N/A",
        },
    },
    "lmstudio": {
        "label": "LM Studio",
        "profile": "lmstudio",
        "updates": {
            "base_url": "http://localhost:1234/v1",
            "model": "local-model",
            "api_key_env": "N/A",
        },
    },
    "llamacpp": {
        "label": "llama.cpp",
        "profile": "llamacpp",
        "updates": {
            "base_url": "http://localhost:8080/v1",
            "model": "local-model",
            "api_key_env": "N/A",
        },
    },
    "custom": {
        "label": "Custom",
        "profile": "custom",
        "updates": {
            "base_url": "http://localhost:8080/v1",
            "model": "your-model",
            "api_key_env": "N/A",
        },
    },
}


class _DimToolLineProcessor(Processor):
    def __init__(self, enabled_getter=None) -> None:
        self._enabled_getter = enabled_getter or (lambda: True)

    def apply_transformation(self, transformation_input) -> Transformation:
        if not self._enabled_getter():
            return Transformation(transformation_input.fragments)
        text = "".join(fragment[1] for fragment in transformation_input.fragments)
        if text.startswith(_DIMMED_LINE_PREFIXES):
            return Transformation(
                [("class:tool-dim", fragment[1]) for fragment in transformation_input.fragments]
            )
        return Transformation(transformation_input.fragments)


@dataclass
class CLITheme:
    colors: dict[str, str] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)

    def c(self, key: str) -> str:
        defaults = {
            "banner": "bright_cyan",
            "tagline": "bright_black",
            "border": "bright_black",
            "thinking": "yellow",
            "tool_call": "bright_black",
            "tool_ok": "green",
            "tool_error": "red",
            "reset": "yellow",
            "error": "red",
        }
        return self.colors.get(key) or defaults.get(key, "")

    def t(self, key: str) -> str:
        defaults = {
            "name": "TinyCTX",
            "tagline": "Agent Framework",
            "bye_message": "Bye.",
        }
        return self.text.get(key) or defaults.get(key, "")


class CLIBridge:
    def __init__(self, gateway, options: dict | None = None) -> None:
        self._gateway = gateway
        self._options = dict(options or {})
        self._theme = CLITheme(
            colors=self._options.get("customcolors") or {},
            text=self._options.get("customtext") or {},
        )
        self._reply_done = asyncio.Event()
        self._cursor: str | None = None
        self._application: Application | None = None
        self._output_area: TextArea | None = None
        self._input_area: TextArea | None = None
        self._welcome_window: Window | None = None
        self._settings_control: FormattedTextControl | None = None
        self._settings_window: Window | None = None
        self._transcript_blocks: list[str] = []
        self._current_stream = ""
        self._thinking = False
        self._status_text = "ready"
        self._send_task: asyncio.Task | None = None
        self._last_output_width: int | None = None
        self._settings_path: list[str] = []
        self._settings_selected: list[int] = []
        self._settings_notice = ""

    def _resolve_runtime_log_level(self) -> int:
        config = getattr(self._gateway, "_config", None)
        configured = getattr(getattr(config, "logging", None), "level", "WARNING")
        raw = self._options.get("log_level")
        if isinstance(raw, str) and raw.lower() == "inherit":
            raw = configured
        elif raw is None:
            raw = "WARNING"
        return resolve_log_level(raw, default=logging.WARNING)

    def _settings_open(self) -> bool:
        return bool(self._settings_path)

    def _option_value(self, key: str, default=None):
        if default is None:
            default = _CLI_OPTION_DEFAULTS.get(key)
        return self._options.get(key, default)

    def _bool_option(self, key: str, default: bool) -> bool:
        return bool(self._option_value(key, default))

    def _string_option(self, key: str, default: str) -> str:
        value = self._option_value(key, default)
        if value is None:
            return default
        return str(value).strip().lower() or default

    def _max_tool_cycles(self) -> int:
        config = getattr(self._gateway, "_config", None)
        return int(getattr(config, "max_tool_cycles", 20) or 20)

    def _profile_names(self) -> list[str]:
        config = getattr(self._gateway, "_config", None)
        models = getattr(config, "models", {}) or {}
        return list(models.keys())

    def _current_primary_profile(self) -> str:
        config = getattr(self._gateway, "_config", None)
        llm = getattr(config, "llm", None)
        primary = getattr(llm, "primary", "")
        return str(primary or "")

    def _model_profile(self, profile_name: str):
        config = getattr(self._gateway, "_config", None)
        models = getattr(config, "models", {}) or {}
        return models.get(profile_name)

    def _provider_label_for_profile(self, profile_name: str) -> str:
        profile = self._model_profile(profile_name)
        if profile is None:
            return "unknown"
        base_url = getattr(profile, "base_url", "") or ""
        for preset in _PROVIDER_PRESETS.values():
            if preset["updates"]["base_url"] == base_url:
                return preset["label"]
        return "custom"

    def _profile_menu_name(self) -> str | None:
        if not self._settings_open():
            return None
        menu_id = self._settings_path[-1]
        if menu_id.startswith("provider_profile:"):
            return menu_id.split(":", 1)[1]
        if menu_id.startswith("provider_preset:"):
            return menu_id.split(":", 1)[1]
        return None

    def _config_source_path(self) -> Path:
        config = getattr(self._gateway, "_config", None)
        path = getattr(config, "_source_path", None)
        if path is None:
            return Path("config.yaml").resolve()
        return Path(path).resolve()

    def _reload_runtime_config(self) -> None:
        fresh = load_config(str(self._config_source_path()))
        config = getattr(self._gateway, "_config", None)
        if config is None:
            setattr(self._gateway, "_config", fresh)
            return
        for attr in (
            "models",
            "llm",
            "router",
            "bridges",
            "gateway",
            "workspace",
            "logging",
            "max_tool_cycles",
            "context",
            "attachments",
            "extra",
        ):
            setattr(config, attr, getattr(fresh, attr))
        setattr(config, "_source_path", getattr(fresh, "_source_path", self._config_source_path()))
        cli_bridge = config.bridges.get("cli")
        if cli_bridge is not None:
            self._options.update(cli_bridge.options)

    def _settings_menu(self) -> tuple[str, list[dict]]:
        menu_id = self._settings_path[-1] if self._settings_path else "root"
        if menu_id == "root":
            return "Settings", [
                {"label": "Providers", "kind": "submenu", "target": "providers"},
                {"label": "Appearance", "kind": "submenu", "target": "appearance"},
                {"label": "Behavior", "kind": "submenu", "target": "behavior"},
                {"label": "Session", "kind": "submenu", "target": "session"},
                {"label": "Close settings", "kind": "action", "action": "close_settings"},
            ]
        if menu_id == "providers":
            current_primary = self._current_primary_profile()
            return "Providers", [
                {
                    "label": "Manage active profile",
                    "kind": "submenu",
                    "target": f"provider_profile:{current_primary}",
                },
                {"label": "Add preset profile", "kind": "submenu", "target": "providers_add"},
                {"label": "Set primary profile", "kind": "submenu", "target": "providers_primary"},
                {"label": "Back", "kind": "action", "action": "back"},
            ]
        if menu_id == "providers_add":
            items = []
            for preset_key, preset in _PROVIDER_PRESETS.items():
                items.append({
                    "label": preset["label"],
                    "kind": "create_preset_profile",
                    "preset_key": preset_key,
                    "profile_name": preset["profile"],
                })
            items.append({"label": "Back", "kind": "action", "action": "back"})
            return "Add Preset Profile", items
        if menu_id == "providers_primary":
            items = []
            current_primary = self._current_primary_profile()
            for profile_name in self._profile_names():
                items.append({
                    "label": profile_name,
                    "kind": "set_primary_profile",
                    "profile_name": profile_name,
                    "selected": profile_name == current_primary,
                })
            items.append({"label": "Back", "kind": "action", "action": "back"})
            return "Set Primary Profile", items
        if menu_id.startswith("provider_profile:"):
            profile_name = menu_id.split(":", 1)[1]
            return f"Profile: {profile_name}", [
                {
                    "label": "Apply preset",
                    "kind": "submenu",
                    "target": f"provider_preset:{profile_name}",
                },
                {
                    "label": "Make primary",
                    "kind": "set_primary_profile",
                    "profile_name": profile_name,
                    "selected": profile_name == self._current_primary_profile(),
                },
                {"label": "Back", "kind": "action", "action": "back"},
            ]
        if menu_id.startswith("provider_preset:"):
            profile_name = menu_id.split(":", 1)[1]
            items = []
            for preset_key, preset in _PROVIDER_PRESETS.items():
                items.append({
                    "label": preset["label"],
                    "kind": "apply_preset_profile",
                    "preset_key": preset_key,
                    "profile_name": profile_name,
                })
            items.append({"label": "Back", "kind": "action", "action": "back"})
            return f"Preset for {profile_name}", items
        if menu_id == "appearance":
            return "Appearance", [
                {
                    "label": "Compact tool lines",
                    "kind": "toggle",
                    "option": "compact_tools",
                    "default": True,
                },
                {
                    "label": "Dim tool lines",
                    "kind": "toggle",
                    "option": "dim_tools",
                    "default": True,
                },
                {
                    "label": "Word wrap",
                    "kind": "toggle",
                    "option": "word_wrap",
                    "default": True,
                },
                {"label": "Back", "kind": "action", "action": "back"},
            ]
        if menu_id == "behavior":
            return "Behavior", [
                {"label": "Agent round trips", "kind": "submenu", "target": "round_trips"},
                {"label": "Log level", "kind": "submenu", "target": "log_level"},
                {
                    "label": "Quiet startup",
                    "kind": "toggle",
                    "option": "quiet_startup",
                    "default": True,
                },
                {"label": "Back", "kind": "action", "action": "back"},
            ]
        if menu_id == "round_trips":
            current = self._max_tool_cycles()
            items = []
            for value in _ROUND_TRIP_CHOICES:
                items.append({
                    "label": str(value),
                    "kind": "set_config",
                    "config_key": "max_tool_cycles",
                    "value": value,
                    "selected": current == value,
                })
            if current not in _ROUND_TRIP_CHOICES:
                items.append({
                    "label": str(current),
                    "kind": "set_config",
                    "config_key": "max_tool_cycles",
                    "value": current,
                    "selected": True,
                })
            items.append({"label": "Back", "kind": "action", "action": "back"})
            return "Agent Round Trips", items
        if menu_id == "log_level":
            current = self._string_option("log_level", "warning")
            items = []
            for level in _LOG_LEVEL_CHOICES:
                items.append({
                    "label": level,
                    "kind": "set",
                    "option": "log_level",
                    "value": level,
                    "selected": current == level,
                })
            items.append({"label": "Back", "kind": "action", "action": "back"})
            return "Log Level", items
        if menu_id == "session":
            return "Session", [
                {"label": "New session", "kind": "action", "action": "reset_session"},
                {"label": "Resume current session", "kind": "action", "action": "resume_session"},
                {"label": "Close settings", "kind": "action", "action": "close_settings"},
                {"label": "Back", "kind": "action", "action": "back"},
            ]
        return "Settings", [{"label": "Back", "kind": "action", "action": "back"}]

    def _settings_items(self) -> list[dict]:
        return self._settings_menu()[1]

    def _settings_selected_index(self) -> int:
        if not self._settings_selected:
            self._settings_selected = [0]
        items = self._settings_items()
        idx = self._settings_selected[-1]
        if not items:
            idx = 0
        else:
            idx = max(0, min(idx, len(items) - 1))
        self._settings_selected[-1] = idx
        return idx

    def _settings_status_text(self) -> str:
        if not self._settings_open():
            return self._status_text
        title, _ = self._settings_menu()
        if title == "Settings":
            return "settings"
        return f"settings / {title.lower()}"

    def _open_settings(self) -> None:
        self._settings_path = ["root"]
        self._settings_selected = [0]
        self._settings_notice = ""
        self._status_text = "settings"
        if self._application is not None and self._settings_control is not None:
            self._application.layout.focus(self._settings_control)
            self._application.invalidate()

    def _close_settings(self) -> None:
        self._settings_path.clear()
        self._settings_selected.clear()
        self._settings_notice = ""
        self._status_text = "ready"
        if self._application is not None:
            if self._input_area is not None:
                self._application.layout.focus(self._input_area)
            self._application.invalidate()

    def _back_settings(self) -> None:
        if not self._settings_open():
            return
        if len(self._settings_path) <= 1:
            self._close_settings()
            return
        self._settings_path.pop()
        self._settings_selected.pop()
        self._settings_notice = ""
        if self._application is not None:
            self._application.invalidate()

    def _move_settings(self, delta: int) -> None:
        if not self._settings_open():
            return
        items = self._settings_items()
        if not items:
            return
        idx = self._settings_selected_index()
        self._settings_selected[-1] = max(0, min(idx + delta, len(items) - 1))
        if self._application is not None:
            self._application.invalidate()

    def _truncate_arg(self, value, max_chars: int = 80) -> str:
        rendered = repr(value)
        if len(rendered) > max_chars:
            return rendered[:max_chars] + "..."
        return rendered

    def _persist_cli_option(self, key: str, value) -> None:
        update_bridge_options("cli", {key: value}, path=self._config_source_path(), enabled=True)

    def _apply_config_value(self, key: str, value, *, notice: str | None = None) -> None:
        update_config_values({key: value}, path=self._config_source_path())
        config = getattr(self._gateway, "_config", None)
        if config is not None:
            setattr(config, key, value)
        if notice:
            self._settings_notice = notice
        else:
            self._settings_notice = f"{key} set to {value!r}"
        self._refresh_output(self._resolve_runtime_log_level())
        if self._application is not None:
            self._application.invalidate()

    def _apply_provider_preset(
        self,
        profile_name: str,
        preset_key: str,
        *,
        set_primary: bool = False,
        notice: str | None = None,
    ) -> None:
        preset = _PROVIDER_PRESETS[preset_key]
        update_model_profile(
            profile_name,
            dict(preset["updates"]),
            path=self._config_source_path(),
            set_primary=set_primary if set_primary else None,
        )
        self._reload_runtime_config()
        self._settings_notice = notice or f"saved {preset['label']} preset to {profile_name}"
        self._refresh_output(self._resolve_runtime_log_level())
        if self._application is not None:
            self._application.invalidate()

    def _apply_primary_profile(self, profile_name: str) -> None:
        set_primary_model(profile_name, path=self._config_source_path())
        self._reload_runtime_config()
        self._settings_notice = f"primary profile set to {profile_name}"
        self._refresh_output(self._resolve_runtime_log_level())
        if self._application is not None:
            self._application.invalidate()

    def _apply_cli_option(self, key: str, value, *, notice: str | None = None) -> None:
        self._options[key] = value
        self._persist_cli_option(key, value)
        if notice:
            self._settings_notice = notice
        else:
            self._settings_notice = f"{key} set to {value!r}"
        self._refresh_output(self._resolve_runtime_log_level())
        if self._application is not None:
            self._application.invalidate()

    def _invoke_settings_action(self, action: str) -> None:
        if action == "close_settings":
            self._close_settings()
            return
        if action == "back":
            self._back_settings()
            return
        if action == "reset_session":
            self._close_settings()
            self._send_task = asyncio.create_task(self._handle_command("/reset"))
            return
        if action == "resume_session":
            self._close_settings()
            self._send_task = asyncio.create_task(self._handle_command("/resume"))

    def _return_to_provider_profile(self, profile_name: str) -> None:
        self._settings_path = ["root", "providers", f"provider_profile:{profile_name}"]
        self._settings_selected = [0, 0, 0]
        if self._application is not None:
            self._application.invalidate()

    def _activate_settings_selection(self) -> None:
        if not self._settings_open():
            return
        items = self._settings_items()
        if not items:
            return
        item = items[self._settings_selected_index()]
        kind = item["kind"]
        if kind == "submenu":
            self._settings_path.append(item["target"])
            self._settings_selected.append(0)
            self._settings_notice = ""
        elif kind == "toggle":
            current = self._bool_option(item["option"], bool(item.get("default", False)))
            new_value = not current
            label = item["label"].lower()
            self._apply_cli_option(
                item["option"],
                new_value,
                notice=f"{label} {'enabled' if new_value else 'disabled'}",
            )
        elif kind == "set":
            self._apply_cli_option(
                item["option"],
                item["value"],
                notice=f"{item['option']} set to {item['value']}",
            )
            if self._settings_path and self._settings_path[-1] == "log_level":
                self._back_settings()
                return
        elif kind == "set_config":
            self._apply_config_value(
                item["config_key"],
                item["value"],
                notice=f"agent round trips set to {item['value']}",
            )
            if self._settings_path and self._settings_path[-1] == "round_trips":
                self._back_settings()
                return
        elif kind == "set_primary_profile":
            self._apply_primary_profile(item["profile_name"])
            if self._settings_path and self._settings_path[-1] == "providers_primary":
                self._back_settings()
                return
        elif kind == "create_preset_profile":
            self._apply_provider_preset(
                item["profile_name"],
                item["preset_key"],
                notice=f"created profile {item['profile_name']}",
            )
            self._return_to_provider_profile(item["profile_name"])
            return
        elif kind == "apply_preset_profile":
            self._apply_provider_preset(
                item["profile_name"],
                item["preset_key"],
                notice=f"applied {item['label']} to {item['profile_name']}",
            )
            self._return_to_provider_profile(item["profile_name"])
            return
        elif kind == "action":
            self._invoke_settings_action(item["action"])
            return
        if self._application is not None:
            self._application.invalidate()

    def _settings_body_height(self) -> int:
        if self._application is None:
            return 18
        rows = self._application.output.get_size().rows
        return max(6, rows - 4)

    def _settings_context_lines(self) -> list[str]:
        if not self._settings_open():
            return []
        menu_id = self._settings_path[-1]
        if menu_id == "providers":
            current_primary = self._current_primary_profile()
            profile = self._model_profile(current_primary)
            if profile is None:
                return []
            return [
                f"active profile {current_primary}",
                f"provider {self._provider_label_for_profile(current_primary)}",
                f"base url {getattr(profile, 'base_url', '')}",
            ]
        profile_name = self._profile_menu_name()
        if profile_name:
            profile = self._model_profile(profile_name)
            if profile is None:
                return [f"profile {profile_name}"]
            return [
                f"profile {profile_name}",
                f"provider {self._provider_label_for_profile(profile_name)}",
                f"model {getattr(profile, 'model', '')}",
                f"base url {getattr(profile, 'base_url', '')}",
                f"api key env {getattr(profile, 'api_key_env', '')}",
            ]
        return []

    def _settings_item_line(self, item: dict, *, selected: bool, width: int) -> str:
        prefix = "› " if selected else "  "
        label = item["label"]
        suffix = ""
        if item["kind"] == "submenu":
            if item.get("target") == "providers":
                suffix = self._current_primary_profile()
                line = f"{prefix}{label}"
                available = max(1, width - len(prefix) - len(suffix) - 1)
                line = prefix + self._fit(label, available)
                return self._fit(f"{line}{' ' * max(1, width - len(line) - len(suffix))}{suffix}", width)
            if item.get("target") == "log_level":
                suffix = self._string_option("log_level", "warning")
                line = f"{prefix}{label}"
                available = max(1, width - len(prefix) - len(suffix) - 1)
                line = prefix + self._fit(label, available)
                return self._fit(f"{line}{' ' * max(1, width - len(line) - len(suffix))}{suffix}", width)
            if item.get("target") == "round_trips":
                suffix = str(self._max_tool_cycles())
                line = f"{prefix}{label}"
                available = max(1, width - len(prefix) - len(suffix) - 1)
                line = prefix + self._fit(label, available)
                return self._fit(f"{line}{' ' * max(1, width - len(line) - len(suffix))}{suffix}", width)
            suffix = ">"
        elif item["kind"] == "toggle":
            suffix = "on" if self._bool_option(item["option"], bool(item.get("default", False))) else "off"
        elif item["kind"] in {"set", "set_config"} and item.get("selected"):
            suffix = "current"
        elif item["kind"] == "set_primary_profile":
            suffix = "current" if item.get("selected") else self._provider_label_for_profile(item["profile_name"])
        elif item["kind"] in {"create_preset_profile", "apply_preset_profile"}:
            suffix = item["profile_name"]
        line = f"{prefix}{label}"
        if not suffix:
            return self._fit(line, width)
        available = max(1, width - len(prefix) - len(suffix) - 1)
        line = prefix + self._fit(label, available)
        return self._fit(f"{line}{' ' * max(1, width - len(line) - len(suffix))}{suffix}", width)

    def _settings_fragments(self):
        width = max(40, self._current_width() - 2)
        title, items = self._settings_menu()
        selected = self._settings_selected_index()
        body_height = self._settings_body_height()
        context_lines = self._settings_context_lines()
        reserved = 5 + len(context_lines) + (1 if self._settings_notice else 0)
        visible_count = max(3, body_height - reserved)
        start = max(0, min(selected - visible_count // 2, max(0, len(items) - visible_count)))
        end = min(len(items), start + visible_count)
        path = " / ".join(part.title() for part in self._settings_path)
        fragments: list[tuple[str, str]] = [
            ("class:menu.header", self._fit(f"settings / {path}", width)),
            ("", "\n"),
            ("class:menu.header", self._fit(title, width)),
            ("", "\n"),
        ]
        for line in context_lines:
            fragments.extend([("class:menu.dim", self._fit(line, width)), ("", "\n")])
        if start > 0:
            fragments.extend([("class:menu.dim", self._fit("  ...", width)), ("", "\n")])
        for idx in range(start, end):
            item = items[idx]
            style = "class:menu.selected" if idx == selected else "class:output-area"
            fragments.extend([(style, self._settings_item_line(item, selected=idx == selected, width=width)), ("", "\n")])
        if end < len(items):
            fragments.extend([("class:menu.dim", self._fit("  ...", width)), ("", "\n")])
        if self._settings_notice:
            fragments.extend([("class:menu.dim", self._fit(self._settings_notice, width)), ("", "\n")])
        fragments.append(("class:menu.dim", self._fit("↑/↓ move · Enter select · Esc back", width)))
        return fragments

    def _compact_path(self, path: Path) -> str:
        try:
            return str(path).replace(str(Path.home()), "~", 1)
        except Exception:
            return str(path)

    def _startup_segments(self, log_level: int) -> list[tuple[str, str]]:
        config = getattr(self._gateway, "_config", None)
        if config is None:
            return [("status", "interactive session ready")]

        workspace = Path(config.workspace.path).expanduser().resolve()
        try:
            primary_model = config.get_model_config(config.llm.primary).model
        except Exception:
            primary_model = config.llm.primary

        memory_cfg = (config.extra.get("memory") or {}) if hasattr(config, "extra") else {}
        embedder = memory_cfg.get("embedding_model") or "bm25"

        heartbeat_cfg = (config.extra.get("heartbeat") or {}) if hasattr(config, "extra") else {}
        heartbeat_every = int(heartbeat_cfg.get("every_minutes", 30) or 0)
        heartbeat_text = f"{heartbeat_every}m" if heartbeat_every > 0 else "off"

        mcp_cfg = (config.extra.get("mcp") or {}) if hasattr(config, "extra") else {}
        mcp_servers = mcp_cfg.get("servers") or {}
        if isinstance(mcp_servers, dict) and mcp_servers:
            mcp_text = str(len(mcp_servers))
        else:
            mcp_text = "off"

        return [
            ("workspace", self._compact_path(workspace)),
            ("model", primary_model),
            ("memory", str(embedder)),
            ("heartbeat", heartbeat_text),
            ("mcp", mcp_text),
            ("logs", f"{logging.getLevelName(log_level).lower()}+"),
        ]

    def _startup_summary(self, log_level: int) -> Text:
        summary = Text(style=self._theme.c("border"))
        for idx, (label, value) in enumerate(self._startup_segments(log_level)):
            if idx:
                summary.append(" · ")
            summary.append(f"{label} ", style=self._theme.c("tagline"))
            summary.append(value, style=self._theme.c("banner"))
        return summary

    def _current_width(self) -> int:
        if self._application is None:
            return 120
        return max(80, self._application.output.get_size().columns)

    def _fit(self, text: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width == 1:
            return "…"
        return text[: width - 1] + "…"

    def _center(self, text: str, width: int) -> str:
        fitted = self._fit(text, width)
        if len(fitted) >= width:
            return fitted
        return fitted.center(width)

    def _welcome_lines(self, log_level: int, width: int) -> list[str]:
        config = getattr(self._gateway, "_config", None)
        segments = dict(self._startup_segments(log_level))
        banner_lines = self._banner_lines(width)
        lines = [
            *banner_lines,
            "",
            self._center(f"{self._theme.t('tagline')}", width),
            "",
            self._center(f"model {segments.get('model', 'unknown')}", width),
            self._center(f"memory {segments.get('memory', 'bm25')}", width),
        ]
        if config is not None:
            workspace = Path(config.workspace.path).expanduser().resolve()
            lines.append(self._center(f"cwd {self._compact_path(workspace)}", width))
        if self._cursor:
            lines.append(self._center(f"thread {self._cursor}", width))
        lines.append(self._center("status ready", width))
        return lines

    def _banner_lines(self, width: int) -> list[str]:
        return [self._center(line, width) for line in _TINYCTX_BANNER]

    def _compose_welcome_text(self, log_level: int, width: int | None = None) -> str:
        pane_width = max(60, (width or self._current_width()) - 4)
        lines = self._welcome_lines(log_level, pane_width)
        return "\n".join(lines).rstrip()

    def _welcome_fragments(self):
        log_level = self._resolve_runtime_log_level()
        pane_width = max(60, self._current_width() - 4)
        lines = self._welcome_lines(log_level, pane_width)
        banner_count = len(_TINYCTX_BANNER)
        fragments: list[tuple[str, str]] = []
        for idx, line in enumerate(lines):
            style = "class:banner" if idx < banner_count else "class:output-area"
            fragments.append((style, line))
            if idx < len(lines) - 1:
                fragments.append(("", "\n"))
        return fragments

    def _titlebar_text(self) -> str:
        width = self._current_width()
        label = f" {self._theme.t('name')} "
        side = max(2, (width - len(label)) // 2)
        line = ("─" * side) + label + ("─" * side)
        return self._fit(line, width)

    def _footer_text(self) -> str:
        width = self._current_width()
        return self._fit(f"working {self._settings_status_text()}", width)

    def _set_status(self, text: str) -> None:
        self._status_text = text.strip() or "ready"

    def _render_user_block(self, text: str) -> str:
        lines = [line.rstrip() for line in text.strip().splitlines()]
        if not lines:
            return "›"
        rendered = [f"› {lines[0]}"]
        rendered.extend(f"  {line}" for line in lines[1:])
        return "\n".join(rendered)

    def _summarize_value(self, value, max_chars: int = 84) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            text = ", ".join(str(item) for item in value if item not in (None, ""))
        else:
            text = str(value).strip()
        if not text:
            return ""
        text = text.replace("\n", " ").strip()
        if len(text) > max_chars:
            return text[: max_chars - 1] + "…"
        return text

    def _tool_arg_summary(self, tool_name: str, args: dict) -> str:
        preferred_keys = {
            "web_search": ("query",),
            "browse_url": ("url", "mode"),
            "navigate": ("url",),
            "shell": ("command",),
            "memory_search": ("query",),
            "spawn_agent": ("prompt", "message"),
            "wait_agent": ("task_id", "targets"),
            "read_file": ("path",),
            "write_file": ("path",),
        }
        for key in preferred_keys.get(tool_name, ()):
            summary = self._summarize_value(args.get(key))
            if summary:
                return summary
        for key, value in args.items():
            summary = self._summarize_value(value)
            if summary:
                if key in {"url", "query", "command", "path", "prompt", "message"}:
                    return summary
                return f"{key}={summary}"
        return ""

    def _tool_call_line(self, tool_name: str, args: dict) -> str:
        if not self._bool_option("compact_tools", True):
            args_str = ", ".join(f"{k}={self._truncate_arg(v)}" for k, v in args.items())
            return f"tool {tool_name}({args_str})"
        summary = self._tool_arg_summary(tool_name, args)
        return f"tool {tool_name} {summary}".rstrip()

    def _tool_result_line(self, tool_name: str, output: str, is_error: bool) -> str:
        preview = self._summarize_value(output, max_chars=96)
        state = "err" if is_error else "ok"
        if not self._bool_option("compact_tools", True):
            return f"{state} {tool_name}: {preview}" if preview else f"{state} {tool_name}"
        if preview:
            return f"{state} {tool_name} {preview}"
        return f"{state} {tool_name}"

    def _append_block(self, text: str) -> None:
        block = text.strip()
        if block:
            self._transcript_blocks.append(block)

    def _compose_output_text(self, log_level: int) -> str:
        blocks = list(self._transcript_blocks)
        if self._current_stream.strip():
            blocks.append(self._current_stream.rstrip())
        elif self._thinking:
            blocks.append("thinking…")
        if not blocks:
            return self._compose_welcome_text(log_level)
        if not self._bool_option("word_wrap", True):
            return "\n\n".join(blocks).rstrip()
        return "\n\n".join(
            self._wrap_text_block(block, self._output_wrap_width()) for block in blocks
        ).rstrip()

    def _has_transcript(self) -> bool:
        return bool(self._transcript_blocks or self._current_stream.strip() or self._thinking)

    def _output_wrap_width(self) -> int:
        return max(24, self._current_width() - 3)

    def _wrap_text_line(self, line: str, width: int) -> str:
        if not line:
            return ""
        if len(line) <= width:
            return line
        initial_indent = ""
        subsequent_indent = ""
        if line.startswith("› "):
            initial_indent = "› "
            subsequent_indent = "  "
            line = line[2:]
        elif line.startswith("  "):
            initial_indent = "  "
            subsequent_indent = "  "
            line = line[2:]
        wrapper = textwrap.TextWrapper(
            width=max(12, width),
            initial_indent=initial_indent,
            subsequent_indent=subsequent_indent,
            break_long_words=False,
            break_on_hyphens=False,
        )
        return wrapper.fill(line)

    def _wrap_text_block(self, block: str, width: int) -> str:
        return "\n".join(self._wrap_text_line(line, width) for line in block.splitlines())

    def _refresh_output(self, log_level: int) -> None:
        if self._output_area is None:
            return
        self._last_output_width = self._output_wrap_width()
        text = self._compose_output_text(log_level)
        self._output_area.buffer.set_document(
            Document(text=text, cursor_position=len(text)),
            bypass_readonly=True,
        )
        if self._application is not None:
            self._application.invalidate()

    def _before_render(self, _app) -> None:
        if self._output_area is None or not self._has_transcript():
            return
        width = self._output_wrap_width()
        if width == self._last_output_width:
            return
        self._last_output_width = width
        text = self._compose_output_text(self._resolve_runtime_log_level())
        self._output_area.buffer.set_document(
            Document(text=text, cursor_position=len(text)),
            bypass_readonly=True,
        )

    def _strip_markup(self, text: str) -> str:
        return _RICH_TAG.sub("", text)

    async def handle_event(self, event) -> None:
        log_level = self._resolve_runtime_log_level()
        if isinstance(event, AgentThinkingChunk):
            if not self._current_stream:
                self._thinking = True
            self._set_status("thinking")
            self._refresh_output(log_level)
        elif isinstance(event, AgentTextChunk):
            self._thinking = False
            self._set_status("replying")
            self._current_stream += event.text
            self._refresh_output(log_level)
        elif isinstance(event, AgentToolCall):
            self._thinking = False
            if self._current_stream.strip():
                self._append_block(self._current_stream)
                self._current_stream = ""
            self._set_status(event.tool_name)
            self._append_block(self._tool_call_line(event.tool_name, event.args))
            self._refresh_output(log_level)
        elif isinstance(event, AgentToolResult):
            self._set_status("ready")
            self._append_block(
                self._tool_result_line(event.tool_name, event.output, event.is_error)
            )
            self._refresh_output(log_level)
        elif isinstance(event, AgentTextFinal):
            final_text = (event.text or self._current_stream).strip()
            self._thinking = False
            self._current_stream = ""
            if final_text:
                self._append_block(final_text)
            self._set_status("ready")
            self._refresh_output(log_level)
            self._reply_done.set()
        elif isinstance(event, AgentError):
            self._thinking = False
            self._current_stream = ""
            self._set_status("error")
            self._append_block(f"error: {event.message}")
            self._refresh_output(log_level)
            self._reply_done.set()

    async def _handle_command(self, text: str) -> None:
        if text.lower() == "/debug heartbeat":
            self._set_status("heartbeat")
            await _debug_heartbeat(
                self._gateway,
                _TranscriptConsoleAdapter(self),
                self._theme.c,
            )
            self._set_status("ready")
            self._refresh_output(self._resolve_runtime_log_level())
            return
        if text.lower() == "/reset":
            from db import ConversationDB

            workspace = Path(self._gateway._config.workspace.path).expanduser().resolve()
            db = ConversationDB(workspace / "agent.db")
            root = db.get_root()
            node = db.add_node(parent_id=root.id, role="system", content="session:cli")
            cursor_file = workspace / "cursors" / "cli"
            cursor_file.write_text(node.id, encoding="utf-8")
            self._cursor = node.id
            self._gateway.reset_lane(self._cursor)
            self._transcript_blocks.clear()
            self._current_stream = ""
            self._thinking = False
            self._set_status("ready")
            self._refresh_output(self._resolve_runtime_log_level())
            return
        if text.lower() == "/resume":
            self._set_status("ready")
            self._append_block("resuming from last session")
            self._refresh_output(self._resolve_runtime_log_level())
            return
        if text.lower() == "/settings":
            self._open_settings()
            return
        if text.lower() == "/help":
            self._set_status("help")
            self._append_block(
                "shortcuts\n"
                "  Enter        send message\n"
                "  /reset       start a new session\n"
                "  /resume      keep using the saved session\n"
                "  /settings    open CLI settings\n"
                "  /debug heartbeat  fire a heartbeat tick now\n"
                "  exit         quit TinyCTX"
            )
            self._refresh_output(self._resolve_runtime_log_level())
            return
        self._set_status("ready")
        self._append_block(f"unknown command: {text}")
        self._refresh_output(self._resolve_runtime_log_level())

    async def _submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.lower() in {"exit", "quit"}:
            if self._application is not None:
                self._application.exit(result=None)
            return
        if text.startswith("/"):
            await self._handle_command(text)
            return

        self._set_status("waiting for response")
        self._append_block(self._render_user_block(text))
        self._refresh_output(self._resolve_runtime_log_level())

        msg = InboundMessage(
            tail_node_id=self._cursor,
            author=CLI_USER,
            content_type=ContentType.TEXT,
            text=text,
            message_id=str(time.time_ns()),
            timestamp=time.time(),
        )
        self._reply_done.clear()
        await self._gateway.push(msg)
        await self._reply_done.wait()
        _persist_cli_cursor(self._gateway, self._cursor)

    def _submit_from_buffer(self) -> None:
        if self._input_area is None:
            return
        if self._send_task is not None and not self._send_task.done():
            return
        text = self._input_area.text
        self._input_area.buffer.reset()
        self._send_task = asyncio.create_task(self._submit_text(text))

    def _style(self) -> Style:
        return Style.from_dict({
            "": "#d7d7d7 bg:#000000",
            "frame.border": "#7f7f7f bg:#000000",
            "frame.label": "bold #d0d0d0 bg:#000000",
            "title": "bold #d0d0d0 bg:#000000",
            "banner": "bold #ff3b30 bg:#000000",
            "output-area": "#d7d7d7 bg:#000000",
            "input-area": "#f5f5f5 bg:#000000",
            "footer": "#b0b0b0 bg:#000000",
            "menu.header": "bold #d0d0d0 bg:#000000",
            "menu.selected": "bold #ff3b30 bg:#000000",
            "menu.dim": "#7f7f7f bg:#000000",
            "rule": "#808080 bg:#000000",
            "tool-dim": "#7f7f7f bg:#000000",
        })

    def _build_application(self) -> Application:
        log_level = self._resolve_runtime_log_level()
        self._output_area = TextArea(
            text=self._compose_output_text(log_level),
            read_only=True,
            focusable=True,
            focus_on_click=True,
            scrollbar=True,
            wrap_lines=False,
            style="class:output-area",
            input_processors=[_DimToolLineProcessor(lambda: self._bool_option("dim_tools", True))],
        )
        self._input_area = TextArea(
            multiline=False,
            prompt="› ",
            height=1,
            style="class:input-area",
        )
        self._settings_control = FormattedTextControl(
            self._settings_fragments,
            focusable=True,
            show_cursor=False,
        )
        self._settings_window = Window(
            content=self._settings_control,
            always_hide_cursor=True,
            wrap_lines=False,
            style="class:output-area",
        )
        self._welcome_window = Window(
            content=FormattedTextControl(self._welcome_fragments),
            always_hide_cursor=True,
            wrap_lines=False,
            style="class:output-area",
        )

        key_bindings = KeyBindings()
        showing_settings = Condition(self._settings_open)

        @key_bindings.add("enter", filter=~showing_settings, eager=True)
        def _submit(_event) -> None:
            self._submit_from_buffer()

        @key_bindings.add("enter", filter=showing_settings, eager=True)
        def _settings_enter(_event) -> None:
            self._activate_settings_selection()

        @key_bindings.add("up", filter=showing_settings, eager=True)
        def _settings_up(_event) -> None:
            self._move_settings(-1)

        @key_bindings.add("down", filter=showing_settings, eager=True)
        def _settings_down(_event) -> None:
            self._move_settings(1)

        @key_bindings.add("pageup", filter=showing_settings, eager=True)
        def _settings_page_up(_event) -> None:
            self._move_settings(-5)

        @key_bindings.add("pagedown", filter=showing_settings, eager=True)
        def _settings_page_down(_event) -> None:
            self._move_settings(5)

        @key_bindings.add("left", filter=showing_settings, eager=True)
        @key_bindings.add("escape", filter=showing_settings, eager=True)
        def _settings_back(_event) -> None:
            self._back_settings()

        @key_bindings.add("c-c", eager=True)
        def _exit(event) -> None:
            event.app.exit(result=None)

        @key_bindings.add("c-d", eager=True)
        def _exit_on_empty(event) -> None:
            if self._input_area is not None and not self._input_area.text:
                event.app.exit(result=None)

        showing_transcript = Condition(self._has_transcript)
        root = HSplit([
            Window(
                content=FormattedTextControl(self._titlebar_text),
                height=1,
                style="class:title",
            ),
            ConditionalContainer(
                self._settings_window,
                filter=showing_settings,
            ),
            ConditionalContainer(
                self._welcome_window,
                filter=~showing_settings & ~showing_transcript,
            ),
            ConditionalContainer(
                self._output_area,
                filter=~showing_settings & showing_transcript,
            ),
            ConditionalContainer(
                Window(height=1, char="─", style="class:rule"),
                filter=~showing_settings,
            ),
            ConditionalContainer(
                self._input_area,
                filter=~showing_settings,
            ),
            ConditionalContainer(
                Window(height=1, char="─", style="class:rule"),
                filter=~showing_settings,
            ),
            Window(
                content=FormattedTextControl(self._footer_text),
                height=1,
                style="class:footer",
            ),
        ])
        return Application(
            layout=Layout(root, focused_element=self._input_area),
            key_bindings=key_bindings,
            full_screen=True,
            mouse_support=True,
            enable_page_navigation_bindings=True,
            style=self._style(),
            before_render=self._before_render,
        )

    async def run(self) -> None:
        log_level = self._resolve_runtime_log_level()
        logging.basicConfig(
            level=log_level,
            format="%(message)s",
            datefmt="[%X]",
            force=True,
        )
        self._gateway.register_platform_handler(Platform.CLI.value, self.handle_event)
        self._cursor = _load_cli_cursor(self._gateway)
        self._application = self._build_application()
        self._refresh_output(log_level)
        try:
            with patch_stdout(raw=True):
                await self._application.run_async()
        finally:
            if self._send_task is not None and not self._send_task.done():
                self._send_task.cancel()
            print(self._theme.t("bye_message"))


class _TranscriptConsoleAdapter:
    def __init__(self, bridge: CLIBridge) -> None:
        self._bridge = bridge

    def print(self, *args, sep: str = " ", end: str = "\n", **_kwargs) -> None:
        text = sep.join(str(arg) for arg in args)
        text = self._bridge._strip_markup(text).strip()
        if end == "" and text:
            self._bridge._current_stream += text
            return
        if text:
            self._bridge._append_block(text)


# --- Debug commands ---

async def _debug_heartbeat(gateway, console, c) -> None:
    """
    /debug heartbeat — immediately fire one heartbeat tick so you can verify
    the module is wired correctly without waiting for the real interval.

    Looks for a running heartbeat task on the event loop by name
    ("heartbeat:<node_id>") and calls _tick() directly on the same lane/tail
    the live task uses, so the test exercises the real code path.
    """
    from modules.heartbeat.__main__ import _tick

    hb_task = next(
        (task for task in asyncio.all_tasks() if task.get_name().startswith("heartbeat:")),
        None,
    )

    agent = getattr(gateway, "_heartbeat_agent", None)

    if agent is None:
        for lane in gateway._lane_router._lanes.values():
            loop = getattr(lane, "loop", None)
            if loop is None and hasattr(lane, "_lane"):
                loop = getattr(lane._lane, "loop", None)
            if loop is not None and getattr(loop, "_heartbeat_lane_node_id", None):
                agent = loop
                break

    if agent is None:
        console.print(f"[{c('error')}]  ✗  heartbeat: could not find agent instance[/{c('error')}]")
        console.print(f"[{c('tool_call')}]     (is the heartbeat module registered?)[/{c('tool_call')}]")
        return

    lane_node_id = getattr(agent, "_heartbeat_lane_node_id", None)
    tail_node_id = getattr(agent, "_heartbeat_cursor_node_id", None)

    if not lane_node_id:
        console.print(
            f"[{c('error')}]  ✗  heartbeat: no cursor found — has the module run at least once?[/{c('error')}]"
        )
        if hb_task:
            console.print(
                f"[{c('tool_call')}]     task exists ({hb_task.get_name()}) but hasn't ticked yet[/{c('tool_call')}]"
            )
        return

    try:
        from modules.heartbeat import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    prompt = cfg.get("prompt", "If nothing needs attention, reply HEARTBEAT_OK.")
    continuation_prompt = cfg.get(
        "continuation_prompt",
        "Continue the task, or reply HEARTBEAT_OK when you are done.",
    )
    ack_max = int(cfg.get("ack_max_chars", 300))
    max_continuations = int(cfg.get("max_continuations", 5))

    console.print(f"[{c('tool_call')}]  ⏱  firing heartbeat tick (lane={lane_node_id[:8]}…)[/{c('tool_call')}]")

    try:
        new_tail = await _tick(
            agent,
            lane_node_id,
            tail_node_id,
            prompt,
            continuation_prompt,
            ack_max,
            max_continuations,
        )
        setattr(agent, "_heartbeat_cursor_node_id", new_tail)
        console.print(f"[{c('tool_ok')}]  ✓  heartbeat tick complete[/{c('tool_ok')}]")
    except Exception as exc:
        console.print(f"[{c('error')}]  ✗  heartbeat tick raised: {exc}[/{c('error')}]")


CLI_USER_ID = "cli-owner"
CLI_USER = UserIdentity(platform=Platform.CLI, user_id=CLI_USER_ID, username="you")


def _load_cli_cursor(gateway) -> str:
    """
    Load (or create) the persistent CLI cursor from workspace/cursors/cli.
    On first run, attaches to the DB global root and persists the new node_id.
    On subsequent runs, resumes from the last known tail node.
    """
    from db import ConversationDB

    workspace = Path(gateway._config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    cursors_dir = workspace / "cursors"
    cursors_dir.mkdir(parents=True, exist_ok=True)
    cursor_file = cursors_dir / "cli"
    db_path = workspace / "agent.db"
    db = ConversationDB(db_path)

    if cursor_file.exists():
        node_id = cursor_file.read_text(encoding="utf-8").strip()
        if db.get_node(node_id) is not None:
            return node_id

    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content="session:cli")
    cursor_file.write_text(node.id, encoding="utf-8")
    return node.id


def _persist_cli_cursor(gateway, anchor_node_id: str) -> None:
    """
    After each turn, read the live tail from the active lane and persist it
    to the cursor file so the next session resumes from the right place.
    """
    try:
        lanes = gateway._lane_router._lanes
        lane = lanes.get(anchor_node_id)
        if lane is None:
            lane = next((entry for entry in lanes.values() if entry.node_id == anchor_node_id), None)
        if lane is None:
            return
        tail = lane.loop._tail_node_id
        if not tail:
            return
        workspace = Path(gateway._config.workspace.path).expanduser().resolve()
        cursor_file = workspace / "cursors" / "cli"
        cursor_file.write_text(tail, encoding="utf-8")
    except Exception:
        pass


async def run(gateway) -> None:
    """Entry point called by main.py loader."""
    options = {}

    config = getattr(gateway, "_config", None)
    if config and hasattr(config, "bridges"):
        bridge_cfg = config.bridges.get("cli")
        if bridge_cfg:
            options = getattr(bridge_cfg, "options", {})

    bridge = CLIBridge(gateway, options=options)
    await bridge.run()


if __name__ == "__main__":
    from config import apply_logging, load as load_config
    from gateway import Gateway

    async def _main():
        cfg = load_config()
        apply_logging(cfg.logging)
        await run(Gateway(config=cfg))

    asyncio.run(_main())

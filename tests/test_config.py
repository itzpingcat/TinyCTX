"""
tests/test_config.py

Tests for the config loader — parsing, validation, error handling, and the
Config dataclass helpers.

No filesystem or network I/O beyond writing a temp YAML file.

Run with:
    pytest tests/test_config.py -v
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from config import (
    load,
    Config,
    ModelConfig,
    BridgeConfig,
    GatewayConfig,
    WorkspaceConfig,
    LoggingConfig,
    LLMRoutingConfig,
    FallbackOnConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def _minimal(extra: str = "") -> str:
    """A valid minimal config snippet."""
    base = textwrap.dedent("""\
        models:
          main:
            base_url: http://localhost:11434/v1
            model: llama3
            api_key_env: N/A
        llm:
          primary: main
    """)
    if not extra:
        return base
    # Dedent extra independently so callers can write naturally-indented
    # triple-quoted strings; then re-indent to 0 (root level) to append cleanly.
    return base + textwrap.dedent(extra).strip() + "\n"


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------

class TestModelConfig:
    def test_defaults(self):
        m = ModelConfig(model="llama3", base_url="http://localhost/v1")
        assert m.kind == "chat"
        assert m.max_tokens == 2048
        assert m.temperature == 0.7
        assert m.is_embedding is False

    def test_embedding_kind(self):
        m = ModelConfig(model="nomic", base_url="http://localhost/v1", kind="embedding")
        assert m.is_embedding is True

    def test_api_key_na_returns_empty(self):
        m = ModelConfig(model="x", base_url="http://x", api_key_env="N/A")
        assert m.api_key == ""

    def test_api_key_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("SOME_MISSING_KEY", raising=False)
        m = ModelConfig(model="x", base_url="http://x", api_key_env="SOME_MISSING_KEY")
        with pytest.raises(EnvironmentError, match="SOME_MISSING_KEY"):
            _ = m.api_key

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "secret-123")
        m = ModelConfig(model="x", base_url="http://x", api_key_env="MY_API_KEY")
        assert m.api_key == "secret-123"


# ---------------------------------------------------------------------------
# BridgeConfig
# ---------------------------------------------------------------------------

class TestBridgeConfig:
    def test_enabled_default_false(self):
        bc = BridgeConfig()
        assert bc.enabled is False

    def test_options_accessible_as_attrs(self):
        bc = BridgeConfig(enabled=True, options={"token_env": "BOT_TOKEN", "dm_enabled": True})
        assert bc.token_env == "BOT_TOKEN"
        assert bc.dm_enabled is True

    def test_missing_option_raises_attribute_error(self):
        bc = BridgeConfig()
        with pytest.raises(AttributeError):
            _ = bc.nonexistent_key

    def test_options_dict_direct_access(self):
        bc = BridgeConfig(options={"allowed_users": [123, 456]})
        assert bc.options["allowed_users"] == [123, 456]


# ---------------------------------------------------------------------------
# GatewayConfig
# ---------------------------------------------------------------------------

class TestGatewayConfig:
    def test_defaults(self):
        gc = GatewayConfig()
        assert gc.enabled is False
        assert gc.host == "127.0.0.1"
        assert gc.port == 8080
        assert gc.api_key == ""

    def test_custom_values(self):
        gc = GatewayConfig(enabled=True, host="0.0.0.0", port=9090, api_key="secret")
        assert gc.enabled is True
        assert gc.port == 9090
        assert gc.api_key == "secret"


# ---------------------------------------------------------------------------
# WorkspaceConfig
# ---------------------------------------------------------------------------

class TestWorkspaceConfig:
    def test_default_path_is_absolute(self):
        wc = WorkspaceConfig()
        assert wc.path.is_absolute()

    def test_tilde_expanded(self):
        wc = WorkspaceConfig(path=Path("~/.tinyctx"))
        assert "~" not in str(wc.path)

    def test_custom_path_resolved(self, tmp_path):
        wc = WorkspaceConfig(path=tmp_path / "workspace")
        assert wc.path.is_absolute()


# ---------------------------------------------------------------------------
# LoggingConfig
# ---------------------------------------------------------------------------

class TestLoggingConfig:
    def test_valid_levels(self):
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            lc = LoggingConfig(level=lvl)
            assert lc.level == lvl

    def test_case_insensitive(self):
        lc = LoggingConfig(level="debug")
        assert lc.level == "DEBUG"

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError, match="Invalid log level"):
            LoggingConfig(level="VERBOSE")


# ---------------------------------------------------------------------------
# Config.get_model_config
# ---------------------------------------------------------------------------

class TestGetModelConfig:
    def _make_config(self):
        return Config(
            models={
                "smart": ModelConfig(model="claude", base_url="https://api.anthropic.com/v1"),
                "local": ModelConfig(model="llama3", base_url="http://localhost/v1"),
                "embed": ModelConfig(model="nomic", base_url="http://localhost/v1", kind="embedding"),
            },
            llm=LLMRoutingConfig(primary="smart"),
        )

    def test_known_name_returned(self):
        cfg = self._make_config()
        m = cfg.get_model_config("local")
        assert m.model == "llama3"

    def test_unknown_name_falls_back_to_primary(self):
        cfg = self._make_config()
        m = cfg.get_model_config("nonexistent")
        assert m.model == "claude"

    def test_primary_missing_raises(self):
        cfg = self._make_config()
        cfg.llm.primary = "gone"
        with pytest.raises(KeyError):
            cfg.get_model_config("also_gone")


# ---------------------------------------------------------------------------
# Config.get_embedding_model
# ---------------------------------------------------------------------------

class TestGetEmbeddingModel:
    def _make_config(self):
        return Config(
            models={
                "smart": ModelConfig(model="claude", base_url="https://api.anthropic.com/v1"),
                "embed": ModelConfig(model="nomic", base_url="http://localhost/v1", kind="embedding"),
            },
            llm=LLMRoutingConfig(primary="smart"),
        )

    def test_returns_embedding_model(self):
        cfg = self._make_config()
        m = cfg.get_embedding_model("embed")
        assert m.is_embedding is True

    def test_chat_model_raises_value_error(self):
        cfg = self._make_config()
        with pytest.raises(ValueError, match="kind='chat'"):
            cfg.get_embedding_model("smart")

    def test_missing_name_raises_key_error(self):
        cfg = self._make_config()
        with pytest.raises(KeyError):
            cfg.get_embedding_model("nonexistent")


# ---------------------------------------------------------------------------
# load() — happy path
# ---------------------------------------------------------------------------

class TestLoadHappyPath:
    def test_minimal_config_loads(self, tmp_path):
        p = _write_config(tmp_path, _minimal())
        cfg = load(str(p))
        assert "main" in cfg.models
        assert cfg.llm.primary == "main"
        assert getattr(cfg, "_source_path") == p.resolve()

    def test_models_parsed(self, tmp_path):
        p = _write_config(tmp_path, _minimal())
        cfg = load(str(p))
        m = cfg.models["main"]
        assert m.model == "llama3"
        assert m.base_url == "http://localhost:11434/v1"
        assert m.api_key_env == "N/A"

    def test_multiple_models(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              fast:
                base_url: https://api.openai.com/v1
                model: gpt-4o-mini
                api_key_env: OPENAI_API_KEY
              local:
                base_url: http://localhost/v1
                model: llama3
                api_key_env: N/A
            llm:
              primary: local
        """)
        cfg = load(str(p))
        assert "fast" in cfg.models
        assert "local" in cfg.models

    def test_embedding_model_parsed(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              main:
                base_url: http://localhost/v1
                model: llama3
                api_key_env: N/A
              embed:
                base_url: http://localhost/v1
                model: nomic-embed-text
                api_key_env: N/A
                kind: embedding
            llm:
              primary: main
        """)
        cfg = load(str(p))
        assert cfg.models["embed"].is_embedding is True

    def test_fallback_chain_parsed(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              primary:
                base_url: https://api.anthropic.com/v1
                model: claude-sonnet
                api_key_env: N/A
              backup:
                base_url: http://localhost/v1
                model: llama3
                api_key_env: N/A
            llm:
              primary: primary
              fallback: [backup]
              fallback_on:
                any_error: true
                http_codes: [429, 500]
        """)
        cfg = load(str(p))
        assert cfg.llm.fallback == ["backup"]
        assert cfg.llm.fallback_on.any_error is True
        assert 429 in cfg.llm.fallback_on.http_codes

    def test_bridges_parsed(self, tmp_path):
        p = _write_config(tmp_path, _minimal("""
            bridges:
              cli:
                enabled: true
              discord:
                enabled: false
                options:
                  token_env: DISCORD_BOT_TOKEN
                  allowed_users: [123456, 789012]
        """))
        cfg = load(str(p))
        assert cfg.bridges["cli"].enabled is True
        assert cfg.bridges["discord"].enabled is False
        assert cfg.bridges["discord"].options["allowed_users"] == [123456, 789012]

    def test_gateway_parsed(self, tmp_path):
        p = _write_config(tmp_path, _minimal("""
            gateway:
              enabled: true
              host: 0.0.0.0
              port: 9090
              api_key: mysecret
        """))
        cfg = load(str(p))
        assert cfg.gateway.enabled is True
        assert cfg.gateway.host == "0.0.0.0"
        assert cfg.gateway.port == 9090
        assert cfg.gateway.api_key == "mysecret"

    def test_workspace_path_expanded(self, tmp_path):
        p = _write_config(tmp_path, _minimal("""
            workspace:
              path: ~/.tinyctx_test
        """))
        cfg = load(str(p))
        assert "~" not in str(cfg.workspace.path)
        assert cfg.workspace.path.is_absolute()

    def test_extra_keys_captured(self, tmp_path):
        p = _write_config(tmp_path, _minimal("""
            memory_search:
              embedding_model: embed
              top_k: 5
            mcp:
              servers: []
        """))
        cfg = load(str(p))
        assert "memory_search" in cfg.extra
        assert cfg.extra["memory_search"]["top_k"] == 5
        assert "mcp" in cfg.extra

    def test_context_window_parsed(self, tmp_path):
        p = _write_config(tmp_path, _minimal("context: 8192"))
        cfg = load(str(p))
        assert cfg.context == 8192

    def test_max_tool_cycles_parsed(self, tmp_path):
        p = _write_config(tmp_path, _minimal("max_tool_cycles: 20"))
        cfg = load(str(p))
        assert cfg.max_tool_cycles == 20

    def test_defaults_applied_when_keys_absent(self, tmp_path):
        p = _write_config(tmp_path, _minimal())
        cfg = load(str(p))
        assert cfg.context == 16384
        assert cfg.max_tool_cycles == 20
        assert cfg.gateway.enabled is False
        assert cfg.logging.level == "INFO"


# ---------------------------------------------------------------------------
# load() — error cases
# ---------------------------------------------------------------------------

class TestLoadErrors:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load(str(tmp_path / "nonexistent.yaml"))

    def test_missing_models_section_raises(self, tmp_path):
        p = _write_config(tmp_path, "llm:\n  primary: main\n")
        with pytest.raises(ValueError, match="models"):
            load(str(p))

    def test_model_missing_base_url_raises(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              main:
                model: llama3
            llm:
              primary: main
        """)
        with pytest.raises(ValueError, match="base_url"):
            load(str(p))

    def test_model_missing_model_name_raises(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              main:
                base_url: http://localhost/v1
            llm:
              primary: main
        """)
        with pytest.raises(ValueError, match="model"):
            load(str(p))

    def test_unknown_model_kind_raises(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              main:
                base_url: http://localhost/v1
                model: llama3
                kind: turbocharged
            llm:
              primary: main
        """)
        with pytest.raises(ValueError, match="kind"):
            load(str(p))

    def test_primary_pointing_to_embedding_model_raises(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              embed:
                base_url: http://localhost/v1
                model: nomic
                kind: embedding
            llm:
              primary: embed
        """)
        with pytest.raises(ValueError, match="Embedding"):
            load(str(p))

    def test_fallback_pointing_to_embedding_raises(self, tmp_path):
        p = _write_config(tmp_path, """
            models:
              main:
                base_url: http://localhost/v1
                model: llama3
                api_key_env: N/A
              embed:
                base_url: http://localhost/v1
                model: nomic
                kind: embedding
                api_key_env: N/A
            llm:
              primary: main
              fallback: [embed]
        """)
        with pytest.raises(ValueError, match="embedding"):
            load(str(p))

    def test_empty_yaml_raises(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("")
        with pytest.raises((ValueError, TypeError)):
            load(str(p))

    def test_invalid_log_level_raises(self, tmp_path):
        p = _write_config(tmp_path, _minimal("logging:\n  level: CHATTY"))
        with pytest.raises(ValueError, match="Invalid log level"):
            load(str(p))


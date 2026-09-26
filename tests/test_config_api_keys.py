import os

import pytest

from TinyCTX.config import ModelConfig, load


def _write_config(path, models):
    lines = ["models:"]
    for name, api_key_env in models:
        lines.extend([
            f"  {name}:",
            "    model: test-model",
            "    base_url: http://localhost",
            f"    api_key_env: {api_key_env}",
        ])
    lines.extend([
        "llm:",
        f"  primary: {models[0][0]}",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def test_models_sharing_api_key_env_resolve_and_reuse_same_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TINYCTX_SHARED_TEST_KEY", "dummy-shared-key")
    path = tmp_path / "config.yaml"
    _write_config(path, [("primary", "TINYCTX_SHARED_TEST_KEY"), ("fallback", "TINYCTX_SHARED_TEST_KEY")])

    config = load(path)

    assert config.models["primary"].api_key == "dummy-shared-key"
    assert config.models["fallback"].api_key == "dummy-shared-key"
    assert "TINYCTX_SHARED_TEST_KEY" not in os.environ


def test_distinct_api_key_envs_remain_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv("TINYCTX_KEY_A", "key-a")
    monkeypatch.setenv("TINYCTX_KEY_B", "key-b")
    path = tmp_path / "config.yaml"
    _write_config(path, [("primary", "TINYCTX_KEY_A"), ("fallback", "TINYCTX_KEY_B")])

    config = load(path)

    assert config.models["primary"].api_key == "key-a"
    assert config.models["fallback"].api_key == "key-b"


def test_missing_api_key_fails_with_model_name(tmp_path, monkeypatch):
    monkeypatch.delenv("TINYCTX_MISSING_KEY", raising=False)
    path = tmp_path / "config.yaml"
    _write_config(path, [("primary", "TINYCTX_MISSING_KEY")])

    with pytest.raises(EnvironmentError, match=r"models\.primary"):
        load(path)


def test_direct_model_access_is_stable_and_consumes_once(monkeypatch):
    monkeypatch.setenv("TINYCTX_DIRECT_KEY", "direct-key")
    model = ModelConfig(model="test-model", base_url="http://localhost", api_key_env="TINYCTX_DIRECT_KEY")

    assert model.api_key == "direct-key"
    assert model.api_key == "direct-key"
    assert "TINYCTX_DIRECT_KEY" not in os.environ

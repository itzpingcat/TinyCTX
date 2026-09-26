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


def test_direct_model_access_is_stable_and_non_destructive(monkeypatch):
    monkeypatch.setenv("TINYCTX_DIRECT_KEY", "direct-key")
    model = ModelConfig(model="test-model", base_url="http://localhost", api_key_env="TINYCTX_DIRECT_KEY")

    assert model.api_key == "direct-key"
    assert model.api_key == "direct-key"
    assert os.environ["TINYCTX_DIRECT_KEY"] == "direct-key"


def test_supported_runtime_and_model_options_load(tmp_path, monkeypatch):
    monkeypatch.setenv("TINYCTX_CFG02_KEY", "dummy")
    path = tmp_path / "config.yaml"
    path.write_text(
        """models:
  primary:
    model: test-model
    base_url: http://localhost
    api_key_env: TINYCTX_CFG02_KEY
    timeout: 17
    budget_tokens: 256
    reasoning_effort: high
    cache_prompts: true
llm:
  primary: primary
  fallback_on:
    any_error: false
    http_codes: [429]
embed_cache_size: 33
max_workers: 4
max_empty_retries: 1
""",
        encoding="utf-8",
    )

    config = load(path)

    model = config.models["primary"]
    assert (model.timeout, model.budget_tokens, model.reasoning_effort, model.cache_prompts) == (17, 256, "high", True)
    assert (config.embed_cache_size, config.max_workers, config.max_empty_retries) == (33, 4, 1)
    assert config.llm.fallback_on.http_codes == [429]

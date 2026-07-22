"""
tests/test_instance.py

Tests for commands/_instance.py — shared instance-directory resolution
and path-derivation helpers.

Run with:
    pytest tests/
"""
from __future__ import annotations

import os

import pytest

from TinyCTX.commands._instance import (
    bridge_tag_for,
    compose_env,
    config_path_for,
    load_instance_env,
    project_name_for,
    resolve_instance_dir,
)


class TestResolveInstanceDir:
    def test_explicit_path_wins(self, tmp_path, monkeypatch):
        explicit_dir = tmp_path / "somewhere" / "explicit"
        # cwd/home point elsewhere entirely, to prove explicit overrides them
        other = tmp_path / "other_cwd"
        other.mkdir(parents=True)
        monkeypatch.chdir(other)
        result = resolve_instance_dir(str(explicit_dir))
        assert result == explicit_dir.expanduser().resolve()

    def test_cwd_literally_named_tinyctx_wins(self, tmp_path, monkeypatch):
        instance = tmp_path / ".tinyctx"
        instance.mkdir()
        monkeypatch.chdir(instance)
        assert resolve_instance_dir() == instance.resolve()

    def test_nested_subdir_resolves_to_ancestor_tinyctx(self, tmp_path, monkeypatch):
        instance = tmp_path / ".tinyctx"
        nested = instance / "workspace" / "skills" / "foo"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert resolve_instance_dir() == instance.resolve()

    def test_nearest_tinyctx_ancestor_wins_not_a_further_one(self, tmp_path, monkeypatch):
        # Two nested .tinyctx-named dirs: outer/.tinyctx and outer/.tinyctx/inner/.tinyctx
        outer_instance = tmp_path / ".tinyctx"
        inner_instance = outer_instance / "inner" / ".tinyctx"
        nested = inner_instance / "workspace"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        # nearest ancestor named .tinyctx should be inner_instance, not outer_instance
        assert resolve_instance_dir() == inner_instance.resolve()

    def test_tinyctx_child_of_cwd_is_picked_up(self, tmp_path, monkeypatch):
        cwd_dir = tmp_path / "project"
        child_instance = cwd_dir / ".tinyctx"
        child_instance.mkdir(parents=True)
        monkeypatch.chdir(cwd_dir)
        assert resolve_instance_dir() == child_instance.resolve()

    def test_fallback_to_home_tinyctx(self, tmp_path, monkeypatch):
        cwd_dir = tmp_path / "no_match_here"
        cwd_dir.mkdir()
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.chdir(cwd_dir)
        monkeypatch.setattr(
            "TinyCTX.commands._instance.Path.home", lambda: fake_home
        )
        expected = (fake_home / ".tinyctx").resolve()
        assert resolve_instance_dir() == expected


class TestConfigPathFor:
    def test_appends_config_yaml(self, tmp_path):
        instance_dir = tmp_path / "instance"
        assert config_path_for(instance_dir) == instance_dir / "config.yaml"


class TestProjectNameFor:
    def test_deterministic_same_input(self, tmp_path):
        d = tmp_path / "instance"
        assert project_name_for(d) == project_name_for(d)

    def test_different_input_different_output(self, tmp_path):
        d1 = tmp_path / "instance1"
        d2 = tmp_path / "instance2"
        assert project_name_for(d1) != project_name_for(d2)

    def test_has_expected_prefix_and_length(self, tmp_path):
        d = tmp_path / "instance"
        name = project_name_for(d)
        assert name.startswith("tinyctx-")
        suffix = name[len("tinyctx-"):]
        assert len(suffix) == 10


class TestBridgeTagFor:
    def test_deterministic_same_input(self, tmp_path):
        d = tmp_path / "instance"
        assert bridge_tag_for(d) == bridge_tag_for(d)

    def test_different_input_different_output(self, tmp_path):
        d1 = tmp_path / "instance1"
        d2 = tmp_path / "instance2"
        assert bridge_tag_for(d1) != bridge_tag_for(d2)

    def test_length_is_six(self, tmp_path):
        d = tmp_path / "instance"
        tag = bridge_tag_for(d)
        assert len(tag) == 6

    def test_fits_under_15_chars_with_affixes(self, tmp_path):
        d = tmp_path / "instance"
        tag = bridge_tag_for(d)
        # docstring notes tag is combined with affixes like br_/_ab/_sb
        for affix in ("br_", "_ab", "_sb"):
            assert len(affix + tag) < 15


class TestComposeEnv:
    def test_keys_and_values_without_port(self, tmp_path):
        instance_dir = tmp_path / "instance"
        env = compose_env(instance_dir)
        assert env["TINYCTX_CONFIG_FILE"] == str(instance_dir / "config.yaml")
        assert env["TINYCTX_WORKSPACE"] == str(instance_dir / "workspace")
        assert env["TINYCTX_DATA"] == str(instance_dir / "data")
        assert env["TINYCTX_INSTANCE"] == project_name_for(instance_dir)
        assert env["TINYCTX_TAG"] == bridge_tag_for(instance_dir)
        assert "TINYCTX_PORT" not in env

    def test_port_included_when_given(self, tmp_path):
        instance_dir = tmp_path / "instance"
        env = compose_env(instance_dir, port=8080)
        assert env["TINYCTX_PORT"] == "8080"


class TestLoadInstanceEnv:
    def test_noop_when_env_file_missing(self, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        # Should not raise even though .env doesn't exist
        assert load_instance_env(instance_dir) is None

    def test_loads_and_overrides_env_vars(self, tmp_path, monkeypatch):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        env_file = instance_dir / ".env"
        env_file.write_text("TINYCTX_TEST_VAR=from_dotenv\n")

        monkeypatch.setenv("TINYCTX_TEST_VAR", "pre_existing_value")
        load_instance_env(instance_dir)
        assert os.environ["TINYCTX_TEST_VAR"] == "from_dotenv"

"""
tests/test_module.py — unit tests for TinyCTX/module.py (Module, ToolError).
"""
from __future__ import annotations

import pytest

from TinyCTX.module import Module, ToolError


class TestDefaultName:
    def test_camelcase_class_name_becomes_snake_case(self):
        class CtxTools(Module):
            pass

        assert CtxTools.name == "ctx_tools"

    def test_single_word_class_name(self):
        class Notes(Module):
            pass

        assert Notes.name == "notes"

    def test_explicit_name_is_not_overridden(self):
        class Weird(Module):
            name = "totally_different"

        assert Weird.name == "totally_different"

    def test_acronym_boundary(self):
        class HTTPClient(Module):
            pass

        assert HTTPClient.name == "http_client"


class TestDefaults:
    def test_settings_defaults_to_empty_dict(self):
        class Plain(Module):
            pass

        assert Plain.settings == {}

    def test_dependencies_defaults_to_empty_tuple(self):
        class Plain(Module):
            pass

        assert Plain.dependencies == ()

    def test_platforms_defaults_to_none(self):
        class Plain(Module):
            pass

        assert Plain.platforms is None

    def test_unsafe_defaults_to_false(self):
        class Plain(Module):
            pass

        assert Plain.unsafe is False


class TestResolveSettings:
    def test_returns_schema_defaults_when_no_extra(self):
        class Notes(Module):
            settings = {"max_note_chars": {"default": 8000, "type": "int"}}

        m = Notes()
        assert m.resolve_settings(None) == {"max_note_chars": 8000}

    def test_extra_overrides_default_for_own_namespace_only(self):
        class Notes(Module):
            settings = {"max_note_chars": {"default": 8000, "type": "int"}}

        m = Notes()
        extra = {"notes": {"max_note_chars": 500}, "other_module": {"max_note_chars": 1}}
        assert m.resolve_settings(extra) == {"max_note_chars": 500}

    def test_extra_for_a_different_module_is_ignored(self):
        class Notes(Module):
            settings = {"max_note_chars": {"default": 8000}}

        m = Notes()
        extra = {"unrelated": {"max_note_chars": 1}}
        assert m.resolve_settings(extra) == {"max_note_chars": 8000}

    def test_empty_settings_schema_resolves_to_empty_dict(self):
        class Plain(Module):
            pass

        assert Plain().resolve_settings({"plain": {"whatever": 1}}) == {}


class TestDependenciesSatisfied:
    def test_no_dependencies_is_always_satisfied(self):
        class Plain(Module):
            pass

        ok, missing = Plain().dependencies_satisfied()
        assert ok is True
        assert missing is None

    def test_real_stdlib_dependency_is_satisfied(self):
        class NeedsJson(Module):
            dependencies = ("json",)

        ok, missing = NeedsJson().dependencies_satisfied()
        assert ok is True
        assert missing is None

    def test_missing_dependency_is_reported_not_raised(self):
        class NeedsGhost(Module):
            dependencies = ("definitely_not_a_real_package_xyz123",)

        ok, missing = NeedsGhost().dependencies_satisfied()
        assert ok is False
        assert missing == "definitely_not_a_real_package_xyz123"


class TestToolError:
    def test_is_an_exception(self):
        with pytest.raises(ToolError):
            raise ToolError("note already exists")

    def test_carries_its_message(self):
        try:
            raise ToolError("note too long")
        except ToolError as e:
            assert str(e) == "note too long"

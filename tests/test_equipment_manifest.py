"""
tests/test_equipment_manifest.py

Tests for modules/equipment_manifest/__init__.py — the EquipmentManifest
Module class (MODULES-PLAN-P1.md P2's other proving target: @prompt paired
with a @hook that feeds it via scratch, and a @hook(STARTUP) doing the
one-time EM.md/Jinja2 setup that used to live in register_runtime() +
register_agent()).

Uses a real ConversationDB(":memory:") + Context; runtime/users are minimal
fakes exposing only what EquipmentManifest reads from them.

Run with:
    pytest tests/test_equipment_manifest.py -v
"""
from __future__ import annotations

import types

import pytest

from TinyCTX.db import ConversationDB
from TinyCTX.context import Context, HistoryEntry
from TinyCTX.module_registry import ModuleRegistry
from TinyCTX.modules.equipment_manifest import EquipmentManifest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    d = ConversationDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def ctx(db):
    root = db.get_root()
    return Context(db, tail_node_id=root.id, token_limit=100_000)


class _FakeUserStore:
    def __init__(self, trusted_usernames=()):
        self._trusted = set(trusted_usernames)

    def get_user(self, username):
        if username not in self._trusted:
            return None
        user = types.SimpleNamespace()
        user.has_permission = lambda perm, cfg: True
        return user


def _make_runtime(workspace, extra=None, trusted_usernames=()):
    return types.SimpleNamespace(
        users=_FakeUserStore(trusted_usernames),
        config=types.SimpleNamespace(
            workspace=types.SimpleNamespace(path=str(workspace)),
            permissions=None,
            extra=extra or {},
            config_path=None,
        ),
    )


def _load(tmp_path, em_body, footer_body=None, extra=None, trusted_usernames=()):
    (tmp_path / "EM.md").write_text(em_body, encoding="utf-8")
    if footer_body is not None:
        (tmp_path / "EM_FOOTER.md").write_text(footer_body, encoding="utf-8")

    instance = EquipmentManifest()
    extra = {"equipment_manifest": {"em_path": str(tmp_path / "EM.md"), **(extra or {})}}
    instance.config = instance.resolve_settings(extra)
    runtime = _make_runtime(tmp_path, trusted_usernames=trusted_usernames)
    instance.load(runtime)
    return instance


def _wire_into(instance, cycle):
    registry = ModuleRegistry()
    registry._wire_module_instance(instance, cycle)


class _FakeCycle:
    def __init__(self, context):
        self.context = context
        self.stream_text_hooks: list = []


def _user(ctx, text, author_id="kamie"):
    return ctx.add(HistoryEntry.user(text, author_id=author_id))


def _system_msg(messages):
    return next((m["content"] for m in messages if m["role"] == "system"), None)


def _user_msgs(messages):
    return [m["content"] for m in messages if m["role"] == "user"]


# ---------------------------------------------------------------------------
# STARTUP / activation
# ---------------------------------------------------------------------------

class TestActivation:
    def test_missing_em_md_leaves_module_inactive(self, tmp_path):
        instance = EquipmentManifest()
        instance.config = instance.resolve_settings(
            {"equipment_manifest": {"em_path": str(tmp_path / "EM.md")}}
        )
        runtime = _make_runtime(tmp_path)
        instance.load(runtime)  # no EM.md written at that path
        assert instance._active is False

    def test_disabled_via_config_leaves_module_inactive(self, tmp_path):
        (tmp_path / "EM.md").write_text("hi", encoding="utf-8")
        instance = EquipmentManifest()
        instance.config = instance.resolve_settings({"equipment_manifest": {"enabled": False}})
        runtime = _make_runtime(tmp_path)
        instance.load(runtime)
        assert instance._active is False

    def test_valid_em_md_activates(self, tmp_path):
        instance = _load(tmp_path, "Workspace: {{ workspace_path }}")
        assert instance._active is True


# ---------------------------------------------------------------------------
# Top prompt (system, cache-stable)
# ---------------------------------------------------------------------------

class TestTopPrompt:
    def test_renders_static_variables(self, tmp_path, ctx):
        instance = _load(tmp_path, "OS: {{ system }}\nWorkspace: {{ workspace_path }}")
        cycle = _FakeCycle(ctx)
        _wire_into(instance, cycle)
        _user(ctx, "hi")

        messages, _ = ctx.assemble()
        system = _system_msg(messages)
        assert system is not None
        assert str(tmp_path) in system

    def test_inactive_module_registers_no_system_prompt(self, tmp_path, ctx):
        instance = EquipmentManifest()
        instance.config = instance.resolve_settings(
            {"equipment_manifest": {"em_path": str(tmp_path / "EM.md")}}
        )
        runtime = _make_runtime(tmp_path)
        instance.load(runtime)  # no EM.md written at that path — inactive
        cycle = _FakeCycle(ctx)
        _wire_into(instance, cycle)
        _user(ctx, "hi")

        messages, _ = ctx.assemble()
        assert _system_msg(messages) is None

    def test_trusted_variable_reflects_permission(self, tmp_path, db, ctx):
        instance = _load(
            tmp_path, "{% if trusted %}YES_TRUSTED{% else %}NO_TRUSTED{% endif %}",
            trusted_usernames=("kamie",),
        )
        cycle = _FakeCycle(ctx)
        _wire_into(instance, cycle)
        node = _user(ctx, "hi", author_id="kamie")
        db.set_state(node.id, "author_id", "kamie")

        messages, _ = ctx.assemble()
        assert _system_msg(messages) == "YES_TRUSTED"


# ---------------------------------------------------------------------------
# Footer prompt (user, volatile) + its feeder PRE_ASSEMBLE hook
# ---------------------------------------------------------------------------

class TestFooterPrompt:
    def test_builtin_footer_used_when_no_footer_file(self, tmp_path, ctx):
        instance = _load(tmp_path, "manifest body")
        cycle = _FakeCycle(ctx)
        _wire_into(instance, cycle)
        _user(ctx, "hi")

        messages, _ = ctx.assemble()
        assert any("<clock>" in m for m in _user_msgs(messages))

    def test_custom_footer_file_used_when_present(self, tmp_path, ctx):
        instance = _load(tmp_path, "manifest body", footer_body="FOOTER: {{ time }}")
        cycle = _FakeCycle(ctx)
        _wire_into(instance, cycle)
        _user(ctx, "hi")

        messages, _ = ctx.assemble()
        assert any(m.startswith("FOOTER:") for m in _user_msgs(messages))

    def test_time_since_last_message_uses_scratch_from_pre_assemble_hook(self, tmp_path, ctx):
        instance = _load(tmp_path, "manifest body", footer_body="elapsed={{ time_since_last_message }}")
        cycle = _FakeCycle(ctx)
        _wire_into(instance, cycle)

        _user(ctx, "first")
        _assistant_reply(ctx)
        _user(ctx, "second")

        messages, _ = ctx.assemble()
        footer = next(m for m in _user_msgs(messages) if m.startswith("elapsed="))
        # A prior user turn exists, so the feeder hook must have found it —
        # the empty-string "no prior message" case must NOT fire here.
        assert footer != "elapsed="

    def test_first_message_in_session_has_no_elapsed_time(self, tmp_path, ctx):
        instance = _load(tmp_path, "manifest body", footer_body="elapsed={{ time_since_last_message }}")
        cycle = _FakeCycle(ctx)
        _wire_into(instance, cycle)
        _user(ctx, "only message")

        messages, _ = ctx.assemble()
        assert any(m.startswith("elapsed=\n") for m in _user_msgs(messages))


def _assistant_reply(ctx, text="ok"):
    return ctx.add(HistoryEntry.assistant(text))

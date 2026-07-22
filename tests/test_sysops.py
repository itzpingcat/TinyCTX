"""
tests/test_sysops.py

Tests for modules/sysops — user/permission management tools, the /model
slash command, and the set_active_model tool for per-branch LLM override.

Uses real UserStore (sqlite, tmp_path) and real ConversationDB (:memory:)
rather than mocks, since both are cheap and give realistic behavior. The
runtime/agent objects themselves are lightweight fakes mirroring the
minimal surface sysops actually touches (mirrors tests/test_tool_handler.py
and tests/test_module_registry.py's _FakeRuntime/_FakeCycle style).

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.contracts import Platform
from TinyCTX.db import ConversationDB
from TinyCTX.users.store import UserStore
from TinyCTX.utils.commands import CommandRegistry
from TinyCTX.utils.tool_handler import ToolCallHandler
from TinyCTX.config.__main__ import LLMRoutingConfig, ModelConfig
from TinyCTX.modules.sysops import __main__ as sysops


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeConfig:
    def __init__(self, primary="main", models=None, extra=None):
        self.llm = LLMRoutingConfig(primary=primary)
        self.models = models if models is not None else {
            "main": ModelConfig(model="m", base_url="http://x"),
            "alt":  ModelConfig(model="m2", base_url="http://x"),
            "embed": ModelConfig(model="e", base_url="http://x", kind="embedding"),
        }
        self.extra = extra if extra is not None else {}


class _FakeContext:
    def __init__(self, tail_node_id):
        self.tail_node_id = tail_node_id


class _FakeRuntime:
    def __init__(self, users, db, config):
        self.users = users
        self.db = db
        self.config = config
        self.commands = CommandRegistry()


class _FakeAgent:
    def __init__(self, caller, db, config, tail_node_id, tool_handler=None):
        self.caller = caller
        self.db = db
        self.config = config
        self.context = _FakeContext(tail_node_id)
        self.tool_handler = tool_handler or ToolCallHandler()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def users(tmp_path):
    return UserStore(data_dir=tmp_path)


@pytest.fixture
def db():
    d = ConversationDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def config():
    return _FakeConfig()


def _make_user(users, level, uid="u1", username_hint="alice"):
    user = users.resolve_user(Platform.DISCORD, uid, username_hint, username_hint.title())
    user.permission_level = level
    users.update_user(user)
    return users.get_user(user.username)


def _node(db):
    root = db.get_root()
    return db.add_node(root.id, "user", "hi").id


def _register(users, db, config, caller_level=100, uid="caller"):
    """Sets up runtime + agent with sysops registered, returns (agent, tool_handler, node_id)."""
    runtime = _FakeRuntime(users, db, config)
    sysops.register_runtime(runtime)
    caller = _make_user(users, caller_level, uid=uid, username_hint=f"caller{uid}")
    node_id = _node(db)
    agent = _FakeAgent(caller, db, config, node_id)
    sysops.register_agent(agent)
    # sysops registers tools deferred (always_on=False) — enable them all so
    # execute_tool_call can reach the closures under test.
    for name in list(agent.tool_handler.tools):
        agent.tool_handler.enable(name)
    return agent, agent.tool_handler, node_id


async def _call(handler, caller, tool_name, **kwargs):
    return await handler.execute_tool_call(
        {"id": "1", "function": {"name": tool_name, "arguments": kwargs}}, caller
    )


# ---------------------------------------------------------------------------
# user_modify_permissions — permission rules
# ---------------------------------------------------------------------------

class TestUserModifyPermissions:
    @pytest.mark.asyncio
    async def test_caller_can_promote_up_to_level_minus_one(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=50)
        target = _make_user(users, 10, uid="t1", username_hint="target1")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=target.username, level=49)
        assert result["success"] is True
        assert "49" in result["result"]

    @pytest.mark.asyncio
    async def test_caller_cannot_promote_to_own_level_or_above(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=50)
        target = _make_user(users, 10, uid="t2", username_hint="target2")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=target.username, level=50)
        assert result["success"] is True  # tool returns a string result either way
        assert "Error" in result["result"]
        assert "may only grant up to level 49" in result["result"]

    @pytest.mark.asyncio
    async def test_caller_cannot_touch_user_at_or_above_own_level(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=50)
        target = _make_user(users, 50, uid="t3", username_hint="target3")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=target.username, level=10)
        assert "Error" in result["result"]
        assert "not below your level" in result["result"]

    @pytest.mark.asyncio
    async def test_level_out_of_range_rejected(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=100)
        target = _make_user(users, 10, uid="t4", username_hint="target4")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=target.username, level=101)
        assert "Error" in result["result"]
        assert "0-100" in result["result"]

    @pytest.mark.asyncio
    async def test_unknown_user_returns_not_found(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=100)
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username="ghost", level=10)
        assert "not found" in result["result"]

    @pytest.mark.asyncio
    async def test_tool_handler_denies_below_min_permission(self, users, db, config):
        """min_permission=50 is enforced by ToolCallHandler itself, independent
        of the closure's own logic."""
        agent, handler, _ = _register(users, db, config, caller_level=100)
        low_caller = _make_user(users, 10, uid="low1", username_hint="lowcaller")
        result = await _call(handler, low_caller, "user_modify_permissions",
                              username="whoever", level=1)
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]


# ---------------------------------------------------------------------------
# user_rename / user_merge — require level 100
# ---------------------------------------------------------------------------

class TestUserRenameMerge:
    @pytest.mark.asyncio
    async def test_rename_allowed_at_level_100(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=100)
        target = _make_user(users, 10, uid="r1", username_hint="renameme")
        result = await _call(handler, agent.caller, "user_rename",
                              username=target.username, new_username="renamed")
        assert result["success"] is True
        assert "Renamed" in result["result"]
        assert users.get_user("renamed") is not None

    @pytest.mark.asyncio
    async def test_rename_denied_below_level_100_by_closure(self, users, db, config):
        """caller_level=99 clears the tool_handler's min_permission=100 gate
        too, so this exercises the ToolCallHandler denial path, not the
        closure's own `if caller_level < 100` branch directly — both exist
        in the source (belt-and-suspenders)."""
        agent, handler, _ = _register(users, db, config, caller_level=99)
        result = await _call(handler, agent.caller, "user_rename",
                              username="whoever", new_username="whatever")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    @pytest.mark.asyncio
    async def test_rename_conflict_returns_error(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=100)
        a = _make_user(users, 10, uid="ra", username_hint="usera")
        b = _make_user(users, 10, uid="rb", username_hint="userb")
        result = await _call(handler, agent.caller, "user_rename",
                              username=a.username, new_username=b.username)
        assert "already taken" in result["result"]

    @pytest.mark.asyncio
    async def test_merge_allowed_at_level_100(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=100)
        primary = _make_user(users, 10, uid="mp", username_hint="primaryuser")
        secondary = _make_user(users, 10, uid="ms", username_hint="secondaryuser")
        result = await _call(handler, agent.caller, "user_merge",
                              primary_username=primary.username,
                              secondary_username=secondary.username)
        assert result["success"] is True
        assert "Merged" in result["result"]
        assert users.get_user(secondary.username) is None

    @pytest.mark.asyncio
    async def test_merge_denied_below_level_100(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=99)
        result = await _call(handler, agent.caller, "user_merge",
                              primary_username="a", secondary_username="b")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]


# ---------------------------------------------------------------------------
# user_list / user_info — min_permission=50, read-only
# ---------------------------------------------------------------------------

class TestUserListInfo:
    @pytest.mark.asyncio
    async def test_user_list_allowed_at_min_permission(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=50)
        result = await _call(handler, agent.caller, "user_list")
        assert result["success"] is True
        assert "user(s)" in result["result"]

    @pytest.mark.asyncio
    async def test_user_list_denied_below_min_permission(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=100)
        low_caller = _make_user(users, 49, uid="low2", username_hint="lowcaller2")
        result = await _call(handler, low_caller, "user_list")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    @pytest.mark.asyncio
    async def test_user_info_unknown_user(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=50)
        result = await _call(handler, agent.caller, "user_info", username="ghost")
        assert result["success"] is True
        assert "not found" in result["result"]

    @pytest.mark.asyncio
    async def test_user_info_known_user(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_level=50)
        result = await _call(handler, agent.caller, "user_info", username=agent.caller.username)
        assert result["success"] is True
        assert agent.caller.username in result["result"]


# ---------------------------------------------------------------------------
# set_active_model tool
# ---------------------------------------------------------------------------

class TestSetActiveModel:
    @pytest.mark.asyncio
    async def test_valid_model_sets_override(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_level=75)
        result = await _call(handler, agent.caller, "set_active_model", name="alt")
        assert result["success"] is True
        assert "alt" in result["result"]
        assert db.get_state(node_id, "model", "") == "alt"

    @pytest.mark.asyncio
    async def test_unknown_model_rejected(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_level=75)
        result = await _call(handler, agent.caller, "set_active_model", name="nonexistent")
        assert result["success"] is True
        assert "Error" in result["result"]
        assert "unknown model" in result["result"]
        # no override written
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_embedding_model_rejected(self, users, db, config):
        """Embedding models are excluded from _chat_model_names, so set_active_model
        should refuse them even though they're a real entry in config.models."""
        agent, handler, node_id = _register(users, db, config, caller_level=75)
        result = await _call(handler, agent.caller, "set_active_model", name="embed")
        assert "unknown model" in result["result"]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_empty_name_clears_override(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_level=75)
        db.set_state(node_id, "model", "alt")
        result = await _call(handler, agent.caller, "set_active_model", name="")
        assert result["success"] is True
        assert "cleared" in result["result"]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_default_keyword_clears_override(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_level=75)
        db.set_state(node_id, "model", "alt")
        result = await _call(handler, agent.caller, "set_active_model", name="default")
        assert "cleared" in result["result"]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_override_persists_for_branch(self, users, db, config):
        """Writes go through db.set_state on the branch's tail node — a
        second read against that same node id sees the persisted value."""
        agent, handler, node_id = _register(users, db, config, caller_level=75)
        await _call(handler, agent.caller, "set_active_model", name="alt")
        # Simulate a later cycle re-reading state for the same branch/node.
        state, _ = db.load_session_state(node_id)
        assert state.get("model") == "alt"

    @pytest.mark.asyncio
    async def test_denied_below_model_min_permission(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_level=100)
        low_caller = _make_user(users, 74, uid="low3", username_hint="lowcaller3")
        result = await _call(handler, low_caller, "set_active_model", name="alt")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_custom_min_permission_from_config_extra(self, users, db):
        """EXTENSION_META.default_config.model_min_permission=75 can be overridden
        per-instance via config.extra['sysops']['model_min_permission']."""
        config = _FakeConfig(extra={"sysops": {"model_min_permission": 10}})
        agent, handler, node_id = _register(users, db, config, caller_level=10)
        result = await _call(handler, agent.caller, "set_active_model", name="alt")
        assert result["success"] is True
        assert db.get_state(node_id, "model", "") == "alt"


# ---------------------------------------------------------------------------
# /model slash command
# ---------------------------------------------------------------------------

class TestModelCommand:
    def _setup(self, users, db, config, caller_level=75, uid="modelcaller"):
        runtime = _FakeRuntime(users, db, config)
        sysops.register_runtime(runtime)
        caller = _make_user(users, caller_level, uid=uid, username_hint=f"mcaller{uid}")
        node_id = _node(db)
        return runtime, caller, node_id

    @pytest.mark.asyncio
    async def test_no_args_shows_status_default(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"node_id": node_id, "caller": caller, "send": sent.append}
        handled = await runtime.commands.dispatch("/model", context)
        assert handled is True
        assert "default" in sent[0]
        assert config.llm.primary in sent[0]

    @pytest.mark.asyncio
    async def test_list_shows_chat_models_only(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model list", context)
        text = sent[0]
        assert "main" in text
        assert "alt" in text
        assert "embed" not in text  # embedding models excluded

    @pytest.mark.asyncio
    async def test_set_valid_model_writes_override(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model alt", context)
        assert "Model override set: alt" in sent[0]
        assert db.get_state(node_id, "model", "") == "alt"

    @pytest.mark.asyncio
    async def test_set_unknown_model_rejected(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model bogus", context)
        assert "Unknown model" in sent[0]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_clear_resets_override(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        db.set_state(node_id, "model", "alt")
        sent = []
        context = {"node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model clear", context)
        assert "cleared" in sent[0]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_status_after_override_shows_override(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        db.set_state(node_id, "model", "alt")
        sent = []
        context = {"node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model", context)
        assert "override" in sent[0]
        assert "alt" in sent[0]

    @pytest.mark.asyncio
    async def test_denied_below_min_permission(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config, caller_level=74)
        sent = []
        context = {"node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model alt", context)
        assert "requires permission level" in sent[0]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_console_reply_path(self, users, db, config):
        """context may provide a sync 'console' with .print() instead of an
        async 'send' (gateway's _StringConsole) — _model_reply must handle both."""
        runtime, caller, node_id = self._setup(users, db, config)

        class _Console:
            def __init__(self):
                self.lines = []
            def print(self, text):
                self.lines.append(text)

        console = _Console()
        context = {"node_id": node_id, "caller": caller, "console": console}
        await runtime.commands.dispatch("/model", context)
        assert len(console.lines) == 1
        assert "default" in console.lines[0]

    @pytest.mark.asyncio
    async def test_no_resolvable_caller_denied(self, users, db, config):
        """No context['caller'], no caller_platform/user_id, and no author_id
        in session state on the node — resolution fails entirely."""
        runtime = _FakeRuntime(users, db, config)
        sysops.register_runtime(runtime)
        node_id = _node(db)
        sent = []
        context = {"node_id": node_id, "send": sent.append}
        await runtime.commands.dispatch("/model", context)
        assert "Cannot resolve your identity" in sent[0]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_caller_resolved_via_platform_and_user_id(self, users, db, config):
        """Discord-style context: caller_platform + caller_user_id instead of
        an already-resolved caller object."""
        runtime = _FakeRuntime(users, db, config)
        sysops.register_runtime(runtime)
        user = _make_user(users, 75, uid="plat1", username_hint="platcaller")
        node_id = _node(db)
        sent = []
        context = {
            "node_id": node_id,
            "caller_platform": "discord",
            "caller_user_id": "plat1",
            "send": sent.append,
        }
        await runtime.commands.dispatch("/model", context)
        assert "default" in sent[0]

    @pytest.mark.asyncio
    async def test_cursor_key_used_when_node_id_absent(self, users, db, config):
        """Discord bridge uses 'cursor' instead of 'node_id'."""
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"cursor": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model", context)
        assert "default" in sent[0]

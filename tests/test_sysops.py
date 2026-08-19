"""
tests/test_sysops.py

Tests for modules/sysops — user/permission management tools, the /model
slash command, and the set_active_model tool for per-branch LLM override.
Rewritten for docs/PERMISSIONS-PLAN.md: permission_level's numeric ceiling
logic ("can only grant up to your level - 1") is gone — user_modify_permissions
now grants/revokes a single permission bool on a user's permission_overrides,
gated on Permission.ROOT (total, no ceiling to enforce). There is a single
global permissions.template (config.yaml) shared by every user; these tests
use an empty template and grant roles purely through permission_overrides,
so what each role can do is explicit at the call site. user_list/user_info
are gated on USER_READ; user_rename/user_merge on ROOT; set_active_model and
/model both on MODEL_SWAP, checked through the same seam.

Uses real UserStore (sqlite, tmp_path), real ConversationDB (:memory:), and
a real PermissionsConfig (so permission resolution is exercised for real)
rather than mocks. The runtime/agent objects themselves are lightweight
fakes mirroring the minimal surface sysops actually touches (mirrors
tests/test_tool_handler.py and tests/test_module_registry.py's
_FakeRuntime/_FakeCycle style).

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.config import PermissionsConfig
from TinyCTX.config.__main__ import LLMRoutingConfig, ModelConfig
from TinyCTX.contracts import Platform
from TinyCTX.db import ConversationDB
from TinyCTX.modules.sysops import __main__ as sysops
from TinyCTX.permissions import Permission
from TinyCTX.tool_handling import ToolCallHandler
from TinyCTX.users.store import UserStore
from TinyCTX.utils.commands import CommandRegistry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeConfig:
    def __init__(self, primary="main", models=None, permissions=None):
        self.llm = LLMRoutingConfig(primary=primary)
        self.models = models if models is not None else {
            "main": ModelConfig(model="m", base_url="http://x"),
            "alt":  ModelConfig(model="m2", base_url="http://x"),
            "embed": ModelConfig(model="e", base_url="http://x", kind="embedding"),
        }
        self.permissions = permissions if permissions is not None else PermissionsConfig()
        self.extra = {}


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


# Test-only stand-ins for the roles the old multi-template system had —
# there's a single global permissions.template now (empty, in _FakeConfig),
# so these are granted per-test-user entirely through permission_overrides.
_ROLE_PERMS: dict[str, frozenset[Permission]] = {
    "guest": frozenset(),
    "member": frozenset({
        Permission.FILE_READ, Permission.NETWORK_READ, Permission.MEMORY_READ,
    }),
    "trusted": frozenset({
        Permission.FILE_READ, Permission.FILE_WRITE,
        Permission.NETWORK_READ, Permission.NETWORK_WRITE,
        Permission.MEMORY_READ, Permission.MEMORY_WRITE,
        Permission.MANAGE_CTX, Permission.MODEL_SWAP,
        Permission.CRON_CREATE, Permission.DM_ACCESS,
        Permission.USER_READ, Permission.IMAGE_GEN,
    }),
    "operator": frozenset(Permission),
}


def _make_user(users, role, uid="u1", username_hint="alice"):
    user = users.resolve_user(Platform.DISCORD, uid, username_hint, username_hint.title())
    user.permission_overrides = {p.value: True for p in _ROLE_PERMS[role]}
    users.update_user(user)
    return users.get_user(user.username)


def _node(db):
    root = db.get_root()
    return db.add_node(root.id, "user", "hi").id


def _register(users, db, config, caller_template="operator", uid="caller"):
    """Sets up runtime + agent with sysops registered, returns (agent, tool_handler, node_id)."""
    runtime = _FakeRuntime(users, db, config)
    sysops.register_runtime(runtime)
    caller = _make_user(users, caller_template, uid=uid, username_hint=f"caller{uid}")
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
# user_modify_permissions — single-bool grant/revoke, ROOT-gated, no ceiling
# ---------------------------------------------------------------------------

class TestUserModifyPermissions:
    @pytest.mark.asyncio
    async def test_root_holder_can_grant_a_permission(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        target = _make_user(users, "guest", uid="t1", username_hint="target1")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=target.username, permission="file_write", value=True)
        assert result["success"] is True
        assert "file_write" in result["result"]
        assert users.get_user(target.username).permission_overrides.get("file_write") is True

    @pytest.mark.asyncio
    async def test_root_holder_can_revoke_a_permission(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        target = _make_user(users, "trusted", uid="t2", username_hint="target2")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=target.username, permission="file_write", value=False)
        assert result["success"] is True
        assert users.get_user(target.username).permission_overrides.get("file_write") is False

    @pytest.mark.asyncio
    async def test_root_holder_can_grant_root_to_self(self, users, db, config):
        """ROOT is total — no 'can only grant up to your own level' ceiling
        left to enforce; a ROOT holder may grant or revoke any bool on
        anyone, including themselves."""
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=agent.caller.username, permission="root", value=True)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_permission_rejected(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        target = _make_user(users, "guest", uid="t4", username_hint="target4")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username=target.username, permission="not-a-real-permission", value=True)
        assert result["success"] is True
        assert "Error" in result["result"]
        assert "unknown permission" in result["result"]

    @pytest.mark.asyncio
    async def test_unknown_user_returns_not_found(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        result = await _call(handler, agent.caller, "user_modify_permissions",
                              username="ghost", permission="memory_read", value=True)
        assert "not found" in result["result"]

    @pytest.mark.asyncio
    async def test_tool_handler_denies_caller_without_root(self, users, db, config):
        """ROOT is enforced by ToolCallHandler itself, independent of the
        closure's own logic — a non-ROOT caller never reaches the body."""
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        low_caller = _make_user(users, "trusted", uid="low1", username_hint="lowcaller")
        result = await _call(handler, low_caller, "user_modify_permissions",
                              username="whoever", permission="root", value=True)
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]
        assert "root" in result["error"]


# ---------------------------------------------------------------------------
# user_rename / user_merge — ROOT-gated
# ---------------------------------------------------------------------------

class TestUserRenameMerge:
    @pytest.mark.asyncio
    async def test_rename_allowed_for_root_holder(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        target = _make_user(users, "guest", uid="r1", username_hint="renameme")
        result = await _call(handler, agent.caller, "user_rename",
                              username=target.username, new_username="renamed")
        assert result["success"] is True
        assert "Renamed" in result["result"]
        assert users.get_user("renamed") is not None

    @pytest.mark.asyncio
    async def test_rename_denied_without_root(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="trusted")
        result = await _call(handler, agent.caller, "user_rename",
                              username="whoever", new_username="whatever")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    @pytest.mark.asyncio
    async def test_rename_conflict_returns_error(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        a = _make_user(users, "guest", uid="ra", username_hint="usera")
        b = _make_user(users, "guest", uid="rb", username_hint="userb")
        result = await _call(handler, agent.caller, "user_rename",
                              username=a.username, new_username=b.username)
        assert "already taken" in result["result"]

    @pytest.mark.asyncio
    async def test_merge_allowed_for_root_holder(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        primary = _make_user(users, "guest", uid="mp", username_hint="primaryuser")
        secondary = _make_user(users, "guest", uid="ms", username_hint="secondaryuser")
        result = await _call(handler, agent.caller, "user_merge",
                              primary_username=primary.username,
                              secondary_username=secondary.username)
        assert result["success"] is True
        assert "Merged" in result["result"]
        assert users.get_user(secondary.username) is None

    @pytest.mark.asyncio
    async def test_merge_denied_without_root(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="trusted")
        result = await _call(handler, agent.caller, "user_merge",
                              primary_username="a", secondary_username="b")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]


# ---------------------------------------------------------------------------
# user_list / user_info — USER_READ-gated, read-only
# ---------------------------------------------------------------------------

class TestUserListInfo:
    @pytest.mark.asyncio
    async def test_user_list_allowed_with_user_read(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="trusted")  # trusted holds USER_READ
        result = await _call(handler, agent.caller, "user_list")
        assert result["success"] is True
        assert "user(s)" in result["result"]

    @pytest.mark.asyncio
    async def test_user_list_denied_without_user_read(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="operator")
        low_caller = _make_user(users, "guest", uid="low2", username_hint="lowcaller2")
        result = await _call(handler, low_caller, "user_list")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    @pytest.mark.asyncio
    async def test_user_list_shows_overrides(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="trusted")
        result = await _call(handler, agent.caller, "user_list")
        assert "override(s)" in result["result"]

    @pytest.mark.asyncio
    async def test_user_info_unknown_user(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="trusted")
        result = await _call(handler, agent.caller, "user_info", username="ghost")
        assert result["success"] is True
        assert "not found" in result["result"]

    @pytest.mark.asyncio
    async def test_user_info_known_user_shows_effective_permissions(self, users, db, config):
        agent, handler, _ = _register(users, db, config, caller_template="trusted")
        result = await _call(handler, agent.caller, "user_info", username=agent.caller.username)
        assert result["success"] is True
        assert agent.caller.username in result["result"]
        assert "effective:" in result["result"]
        assert "model_swap" in result["result"]  # trusted holds MODEL_SWAP


# ---------------------------------------------------------------------------
# set_active_model tool — MODEL_SWAP-gated
# ---------------------------------------------------------------------------

class TestSetActiveModel:
    @pytest.mark.asyncio
    async def test_valid_model_sets_override(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_template="trusted")
        result = await _call(handler, agent.caller, "set_active_model", name="alt")
        assert result["success"] is True
        assert "alt" in result["result"]
        assert db.get_state(node_id, "model", "") == "alt"

    @pytest.mark.asyncio
    async def test_unknown_model_rejected(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_template="trusted")
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
        agent, handler, node_id = _register(users, db, config, caller_template="trusted")
        result = await _call(handler, agent.caller, "set_active_model", name="embed")
        assert "unknown model" in result["result"]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_empty_name_clears_override(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_template="trusted")
        db.set_state(node_id, "model", "alt")
        result = await _call(handler, agent.caller, "set_active_model", name="")
        assert result["success"] is True
        assert "cleared" in result["result"]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_default_keyword_clears_override(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_template="trusted")
        db.set_state(node_id, "model", "alt")
        result = await _call(handler, agent.caller, "set_active_model", name="default")
        assert "cleared" in result["result"]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_override_persists_for_branch(self, users, db, config):
        """Writes go through db.set_state on the branch's tail node — a
        second read against that same node id sees the persisted value."""
        agent, handler, node_id = _register(users, db, config, caller_template="trusted")
        await _call(handler, agent.caller, "set_active_model", name="alt")
        # Simulate a later cycle re-reading state for the same branch/node.
        state, _ = db.load_session_state(node_id)
        assert state.get("model") == "alt"

    @pytest.mark.asyncio
    async def test_denied_without_model_swap(self, users, db, config):
        agent, handler, node_id = _register(users, db, config, caller_template="operator")
        low_caller = _make_user(users, "member", uid="low3", username_hint="lowcaller3")  # member lacks MODEL_SWAP
        result = await _call(handler, low_caller, "set_active_model", name="alt")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]
        assert db.get_state(node_id, "model", "") == ""


# ---------------------------------------------------------------------------
# /model slash command — same MODEL_SWAP bool, checked at the CommandRegistry
# seam (docs/PERMISSIONS-PLAN.md §9's whole point: two entry points, one bool)
# ---------------------------------------------------------------------------

class TestModelCommand:
    def _setup(self, users, db, config, caller_template="trusted", uid="modelcaller"):
        runtime = _FakeRuntime(users, db, config)
        sysops.register_runtime(runtime)
        caller = _make_user(users, caller_template, uid=uid, username_hint=f"mcaller{uid}")
        node_id = _node(db)
        return runtime, caller, node_id

    @pytest.mark.asyncio
    async def test_no_args_shows_status_default(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "send": sent.append}
        handled = await runtime.commands.dispatch("/model", context)
        assert handled is True
        assert "default" in sent[0]
        assert config.llm.primary in sent[0]

    @pytest.mark.asyncio
    async def test_list_shows_chat_models_only(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model list", context)
        text = sent[0]
        assert "main" in text
        assert "alt" in text
        assert "embed" not in text  # embedding models excluded

    @pytest.mark.asyncio
    async def test_set_valid_model_writes_override(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model alt", context)
        assert "Model override set: alt" in sent[0]
        assert db.get_state(node_id, "model", "") == "alt"

    @pytest.mark.asyncio
    async def test_set_unknown_model_rejected(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model bogus", context)
        assert "Unknown model" in sent[0]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_clear_resets_override(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        db.set_state(node_id, "model", "alt")
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model clear", context)
        assert "cleared" in sent[0]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_status_after_override_shows_override(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config)
        db.set_state(node_id, "model", "alt")
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model", context)
        assert "override" in sent[0]
        assert "alt" in sent[0]

    @pytest.mark.asyncio
    async def test_denied_without_model_swap(self, users, db, config):
        runtime, caller, node_id = self._setup(users, db, config, caller_template="member")  # lacks MODEL_SWAP
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "send": sent.append}
        handled = await runtime.commands.dispatch("/model alt", context)
        assert handled is True
        assert "PERMISSION DENIED" in sent[0]
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
        context = {"runtime": runtime, "node_id": node_id, "caller": caller, "console": console}
        await runtime.commands.dispatch("/model", context)
        assert len(console.lines) == 1
        assert "default" in console.lines[0]

    @pytest.mark.asyncio
    async def test_no_resolvable_caller_denied(self, users, db, config):
        """No context['caller'] and no caller_platform/user_id — the
        CommandRegistry seam denies before _cmd_model ever runs (it no
        longer has its own caller-resolution fallback)."""
        runtime = _FakeRuntime(users, db, config)
        sysops.register_runtime(runtime)
        node_id = _node(db)
        sent = []
        context = {"runtime": runtime, "node_id": node_id, "send": sent.append}
        handled = await runtime.commands.dispatch("/model", context)
        assert handled is True
        assert "PERMISSION DENIED" in sent[0]
        assert "could not resolve caller" in sent[0]
        assert db.get_state(node_id, "model", "") == ""

    @pytest.mark.asyncio
    async def test_caller_resolved_via_platform_and_user_id(self, users, db, config):
        """Discord-style context: caller_platform + caller_user_id instead of
        an already-resolved caller object."""
        runtime = _FakeRuntime(users, db, config)
        sysops.register_runtime(runtime)
        _make_user(users, "trusted", uid="plat1", username_hint="platcaller")
        node_id = _node(db)
        sent = []
        context = {
            "runtime": runtime,
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
        context = {"runtime": runtime, "cursor": node_id, "caller": caller, "send": sent.append}
        await runtime.commands.dispatch("/model", context)
        assert "default" in sent[0]

    def test_model_command_declares_required_permissions(self, users, db, config):
        """assert_permissions_declared() must not trip on /model — it
        declares required_permissions={MODEL_SWAP} explicitly."""
        runtime = _FakeRuntime(users, db, config)
        sysops.register_runtime(runtime)
        runtime.commands.assert_permissions_declared()  # must not raise

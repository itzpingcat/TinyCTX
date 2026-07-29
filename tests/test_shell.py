"""
tests/test_shell.py

Tests for modules/shell — the shell tool's permission-tiered access, end to end
through the registered tool.

Covers:
  - The backend_access permission-resolution bug fix. The old check read
    agent.config.permissions.level, an attribute PermissionsConfig never
    defines (it only has minimal_tokens: bool) — so backend_access=True
    either crashed or (depending on how the AttributeError surfaced)
    never actually gated anything on the real caller. The fix reads
    agent.caller.permission_level instead, mirroring modules/sysops's
    caller_level snapshot pattern.
  - Tier routing: which policy file applies at which caller level, and that
    bypass_blacklist skips policy checks entirely.
  - Fail-closed behaviour when a policy file won't load. Note this is the
    OPPOSITE of the old blacklist.txt, where a missing file logged a warning
    and left the shell unrestricted.
  - The extra.shell.permissions and extra.shell.policy config blocks.

Rule-level behaviour (what each rule does and doesn't catch) lives in
tests/test_shell_policy.py, against the real shipped YAML. Here the policies
are throwaway fixtures so the tier plumbing is tested independently of rule
content drifting over time.

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.modules.shell import __main__ as shell_mod
from TinyCTX.modules.shell import policy as policy_mod
from TinyCTX.utils.tool_handler import ToolCallHandler

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeWorkspace:
    def __init__(self, path):
        self.path = str(path)


class _FakeConfig:
    def __init__(self, workspace_path, extra=None):
        self.workspace = _FakeWorkspace(workspace_path)
        self.extra = extra if extra is not None else {}


class _FakeCaller:
    def __init__(self, permission_level, username="caller"):
        self.permission_level = permission_level
        self.username = username


class _FakeAgent:
    def __init__(self, caller, config, tool_handler=None):
        self.caller = caller
        self.config = config
        self.tool_handler = tool_handler or ToolCallHandler()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONSTRUCTS = """
constructs:
  program: allow
  command: allow
  command_name: allow
  word: allow
  number: allow
  string: allow
  string_content: allow
  raw_string: allow
  pipeline: allow
  list: allow
"""


def _policy_files(tmp_path, deny_rules="", allow_rules=""):
    """Write throwaway deny/allow policies so these tests don't depend on
    (or break with) the real deny.yaml / allow.yaml contents."""
    deny = tmp_path / "deny.yaml"
    deny.write_text(f"default_action: allow\n{_CONSTRUCTS}rules:\n{deny_rules or '  []'}\n")
    allow = tmp_path / "allow.yaml"
    allow.write_text(f"default_action: deny\n{_CONSTRUCTS}rules:\n{allow_rules or '  []'}\n")
    return deny, allow


def _register(tmp_path, caller_level, extra_shell=None, deny_rules="", allow_rules=""):
    deny, allow = _policy_files(tmp_path, deny_rules, allow_rules)
    extra = {
        "shell": {
            "sandbox_url": None,
            "policy": {"deny": str(deny), "allow": str(allow)},
            **(extra_shell or {}),
        }
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = _FakeConfig(workspace, extra=extra)
    caller = _FakeCaller(caller_level)
    agent = _FakeAgent(caller, config)
    shell_mod.register_agent(agent)
    return agent


async def _call(agent, **kwargs):
    return await agent.tool_handler.execute_tool_call(
        {"id": "1", "function": {"name": "shell", "arguments": kwargs}}, agent.caller,
    )


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    policy_mod.clear_cache()
    yield
    policy_mod.clear_cache()


_ECHO_ALLOWED = "  - {id: echo, action: allow, command: echo, max_args: 4}\n"
_BLOCK_MARKER = "  - {id: no-dd, action: deny, command: dd, message: raw disk tool}\n"


# ---------------------------------------------------------------------------
# backend_access permission-resolution bug fix
# ---------------------------------------------------------------------------

class TestBackendAccessBugFix:
    @pytest.mark.asyncio
    async def test_backend_access_denied_below_threshold_does_not_crash(self, tmp_path):
        # Old code read agent.config.permissions.level, which doesn't exist
        # -> would raise AttributeError instead of a clean denial.
        agent = _register(tmp_path, caller_level=50)
        result = await _call(agent, command="pwd", backend_access=True)
        assert result["success"] is True
        assert "Blocked" in result["result"]
        assert "80" in result["result"]
        assert "50" in result["result"]

    @pytest.mark.asyncio
    async def test_backend_access_allowed_at_threshold(self, tmp_path):
        agent = _register(tmp_path, caller_level=80)
        result = await _call(agent, command="echo backend-ok", backend_access=True)
        assert result["success"] is True
        assert "Blocked" not in result["result"]
        assert "backend-ok" in result["result"]

    @pytest.mark.asyncio
    async def test_backend_access_threshold_is_configurable(self, tmp_path):
        agent = _register(
            tmp_path, caller_level=90,
            extra_shell={"permissions": {"access_backend": 95}},
        )
        result = await _call(agent, command="pwd", backend_access=True)
        assert "Blocked" in result["result"]
        assert "95" in result["result"]

    @pytest.mark.asyncio
    async def test_backend_access_still_policy_checked(self, tmp_path):
        agent = _register(tmp_path, caller_level=85, deny_rules=_BLOCK_MARKER)
        result = await _call(agent, command="dd if=/dev/zero of=x", backend_access=True)
        assert "Blocked" in result["result"]
        assert "no-dd" in result["result"]


# ---------------------------------------------------------------------------
# Tier routing
# ---------------------------------------------------------------------------

class TestTierRouting:
    @pytest.mark.asyncio
    async def test_at_neutral_runs_arbitrary_commands(self, tmp_path):
        agent = _register(tmp_path, caller_level=45)  # default neutral
        result = await _call(agent, command="echo neutral-ok")
        assert "neutral-ok" in result["result"]

    @pytest.mark.asyncio
    async def test_below_neutral_blocked_without_allow_rule(self, tmp_path):
        agent = _register(tmp_path, caller_level=20, allow_rules=_ECHO_ALLOWED)
        result = await _call(agent, command="cat /etc/hosts")
        assert result["success"] is True
        assert "Blocked" in result["result"]
        assert "allow-list" in result["result"]

    @pytest.mark.asyncio
    async def test_below_neutral_allowed_with_allow_rule(self, tmp_path):
        agent = _register(tmp_path, caller_level=20, allow_rules=_ECHO_ALLOWED)
        result = await _call(agent, command="echo hi")
        assert "Blocked" not in result["result"]
        assert "hi" in result["result"]

    @pytest.mark.asyncio
    async def test_allow_rule_does_not_cover_a_chained_command(self, tmp_path):
        # The old whitelist anchored on the whole string to stop this. The AST
        # version gets it for free: `date` is a second command and has no rule.
        agent = _register(tmp_path, caller_level=20, allow_rules=_ECHO_ALLOWED)
        result = await _call(agent, command="echo hi; date")
        assert "Blocked" in result["result"]

    @pytest.mark.asyncio
    async def test_allow_tier_argument_may_contain_metacharacters(self, tmp_path):
        # Replaces the old {arg} character class: quoted text is a parser leaf,
        # so the caller gets their punctuation back without any injection risk.
        agent = _register(tmp_path, caller_level=20, allow_rules=_ECHO_ALLOWED)
        result = await _call(agent, command='echo "it is 5pm; all fine, right?"')
        assert "Blocked" not in result["result"]
        assert "all fine" in result["result"]

    @pytest.mark.asyncio
    async def test_allow_tier_is_still_deny_checked(self, tmp_path):
        agent = _register(
            tmp_path, caller_level=20,
            allow_rules="  - {id: dd-ok, action: allow, command: dd, max_args: 2}\n",
            deny_rules=_BLOCK_MARKER,
        )
        result = await _call(agent, command="dd if=x of=y")
        assert "Blocked" in result["result"]
        assert "no-dd" in result["result"]

    @pytest.mark.asyncio
    async def test_below_use_whitelist_denied_at_framework_level(self, tmp_path):
        agent = _register(tmp_path, caller_level=5)  # below default use_whitelist=10
        result = await _call(agent, command="echo nope")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    def test_registered_min_permission_matches_use_whitelist_config(self, tmp_path):
        agent = _register(
            tmp_path, caller_level=100,
            extra_shell={"permissions": {"use_whitelist": 33}},
        )
        assert agent.tool_handler.tools["shell"]["min_permission"] == 33


# ---------------------------------------------------------------------------
# Deny rules + bypass
# ---------------------------------------------------------------------------

class TestDenyAndBypass:
    @pytest.mark.asyncio
    async def test_deny_rule_blocks_matching_command(self, tmp_path):
        agent = _register(tmp_path, caller_level=89, deny_rules=_BLOCK_MARKER)
        result = await _call(agent, command="dd if=/dev/zero of=x")
        assert "Blocked" in result["result"]
        assert "no-dd" in result["result"]
        assert "raw disk tool" in result["result"]

    @pytest.mark.asyncio
    async def test_bypass_skips_check_at_threshold(self, tmp_path):
        agent = _register(tmp_path, caller_level=90, deny_rules=_BLOCK_MARKER)
        result = await _call(agent, command="echo dd-bypassed")
        assert "Blocked" not in result["result"]
        assert "dd-bypassed" in result["result"]

    @pytest.mark.asyncio
    async def test_warn_rule_runs_and_prefixes_output(self, tmp_path):
        agent = _register(
            tmp_path, caller_level=50,
            deny_rules="  - {id: loud, action: warn, command: echo, message: heads up}\n",
        )
        result = await _call(agent, command="echo still-ran")
        assert "Blocked" not in result["result"]
        assert "heads up" in result["result"]
        assert "still-ran" in result["result"]


# ---------------------------------------------------------------------------
# Fail-closed policy loading
# ---------------------------------------------------------------------------

class TestPolicyLoadFailure:
    @pytest.mark.asyncio
    async def test_missing_policy_blocks_everything(self, tmp_path):
        """The old blacklist.txt did the opposite — a missing file logged a
        warning and left the shell completely unrestricted."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        extra = {"shell": {
            "sandbox_url": None,
            "policy": {"deny": str(tmp_path / "nope.yaml"), "allow": str(tmp_path / "nope.yaml")},
        }}
        agent = _FakeAgent(_FakeCaller(50), _FakeConfig(workspace, extra=extra))
        shell_mod.register_agent(agent)

        result = await _call(agent, command="echo hi")
        assert "Blocked" in result["result"]
        assert "could not be loaded" in result["result"]

    @pytest.mark.asyncio
    async def test_bypass_tier_unaffected_by_broken_policy(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        extra = {"shell": {
            "sandbox_url": None,
            "policy": {"deny": str(tmp_path / "nope.yaml"), "allow": str(tmp_path / "nope.yaml")},
        }}
        agent = _FakeAgent(_FakeCaller(95), _FakeConfig(workspace, extra=extra))
        shell_mod.register_agent(agent)

        result = await _call(agent, command="echo bypass-ok")
        assert "bypass-ok" in result["result"]


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

class TestDefaultPermissions:
    def test_defaults_applied_when_unconfigured(self, tmp_path):
        agent = _register(tmp_path, caller_level=100)
        assert agent.tool_handler.tools["shell"]["min_permission"] == 10

    def test_shipped_policy_files_are_the_default(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        agent = _FakeAgent(
            _FakeCaller(50),
            _FakeConfig(workspace, extra={"shell": {"sandbox_url": None}}),
        )
        shell_mod.register_agent(agent)  # must not raise — shipped YAML loads
        assert "shell" in agent.tool_handler.tools


# ---------------------------------------------------------------------------
# Exit-code annotation
# ---------------------------------------------------------------------------

class TestExitAnnotation:
    def test_last_command_of_a_pipeline_wins(self):
        assert shell_mod.validate.last_command_name("find . -name x | head") == "head"

    def test_pipe_inside_a_quoted_argument_is_not_a_pipe(self):
        # The old _last_cmd() split the raw string on "|" and would have
        # answered "wc" here.
        assert shell_mod.validate.last_command_name('grep "a | b" file') == "grep"

    def test_grep_exit_1_is_not_an_error(self):
        assert shell_mod._annotate_exit("grep foo file", 1) == "(no matches found)"

    def test_grep_exit_2_is_an_error(self):
        assert shell_mod._annotate_exit("grep foo file", 2) == "(exit 2)"

    def test_unknown_command_falls_back_to_exit_code(self):
        assert shell_mod._annotate_exit("mycmd", 3) == "(exit 3)"

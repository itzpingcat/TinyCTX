"""
tests/test_shell.py

End-to-end tests for modules/shell — the registered `shell` tool, through
ToolCallHandler.execute_tool_call, covering the single-gate model
docs/PERMISSIONS-PLAN.md §5/§5.2 replaced the old applies_below tiers with:

  CAPABILITY — modules/shell/perms.py classifies each resolved command into
  the Permission bools it needs; shell's required_permissions callable
  (required_permissions_for_shell) is checked once, centrally, by
  ToolCallHandler.execute_tool_call, exactly like every other tool.
  Per-command tag-table coverage lives in tests/test_shell_perms.py — this
  file exercises the seam end to end (a caller without the right capability
  gets denied before shell() ever runs), not the table itself.

  Construct/shape denial is folded into that same classification: a denied
  construct (`$()`, unquoted globs used as a command, unrecognized bash
  syntax) requires Permission.UNTRUSTED_EXEC, the same as an unrecognized
  command. There is no second check after ToolCallHandler — a caller without
  UNTRUSTED_EXEC gets PERMISSION DENIED before shell() ever runs, same as any
  other worst-cased command; a caller who holds it may actually run the
  construct. modules/shell/__main__.py's _dispatch only re-parses for
  runtime diagnostics that were never permission decisions (empty command,
  over the byte limit, unparseable syntax). Rule-level behaviour of the real
  shipped policy files lives in tests/test_shell_policy.py; this file uses
  throwaway constructs-only policy text so shape-check plumbing is tested
  independently of rule content drifting over time.

Also covers: fail-closed behaviour when the shape policy won't load, and the
backend_access -> BACKEND_EXEC location-permission wiring (§5, "backend_access
no longer compares against a scalar level").

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.modules.shell import __main__ as shell_mod
from TinyCTX.modules.shell import policy as policy_mod
from TinyCTX.permissions import Permission
from TinyCTX.tool_handling import ToolCallHandler

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
    """granted_permissions=None holds every Permission (mirrors an
    unrestricted operator) so tests that only care about shape-checking,
    not capability-gating, don't need to enumerate a grant set."""
    def __init__(self, granted_permissions=None, username="caller"):
        self._granted = (
            frozenset(granted_permissions) if granted_permissions is not None
            else frozenset(Permission)
        )
        self.username = username

    def effective_permissions(self, permissions_config=None):
        return self._granted


class _FakeAgent:
    def __init__(self, caller, config, tool_handler=None):
        self.caller = caller
        self.config = config
        self.tool_handler = tool_handler or ToolCallHandler()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Enough constructs for ordinary commands, pipelines, and redirects — NOT a
# copy of the real allow.yaml (that's what test_shell_policy.py guards).
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
  redirected_statement: allow
  file_redirect: allow
"""


def _register(tmp_path, granted_permissions=None, extra_shell=None, sandbox_url=None):
    extra = {
        "shell": {
            "sandbox_url": sandbox_url,
            **(extra_shell or {}),
        }
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = _FakeConfig(workspace, extra=extra)
    caller = _FakeCaller(granted_permissions)
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


# ---------------------------------------------------------------------------
# Capability gating (the ToolCallHandler seam, not the tag table itself —
# see test_shell_perms.py for that)
# ---------------------------------------------------------------------------

class TestCapabilityGating:
    @pytest.mark.asyncio
    async def test_caller_without_untrusted_exec_denied_unlisted_command(self, tmp_path):
        """python3 isn't in perms.py's minimal table, so it needs
        UNTRUSTED_EXEC — a caller without it must be denied before shell()
        even runs (no 'Blocked:' shape message, a PERMISSION DENIED)."""
        agent = _register(tmp_path, granted_permissions=set())
        result = await _call(agent, command="python3 script.py")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    @pytest.mark.asyncio
    async def test_caller_with_untrusted_exec_may_run_unlisted_command(self, tmp_path):
        agent = _register(tmp_path, granted_permissions={Permission.UNTRUSTED_EXEC})
        result = await _call(agent, command="echo hi")  # pure-compute, needs nothing anyway
        assert result["success"] is True
        assert "hi" in result["result"]

    @pytest.mark.asyncio
    async def test_pure_compute_command_needs_no_capability(self, tmp_path):
        agent = _register(tmp_path, granted_permissions=set())  # holds nothing
        result = await _call(agent, command="echo hi")
        assert result["success"] is True
        assert "hi" in result["result"]

    @pytest.mark.asyncio
    async def test_file_read_command_needs_file_read(self, tmp_path):
        agent = _register(tmp_path, granted_permissions=set())
        result = await _call(agent, command="ls")
        assert result["success"] is False
        assert "file_read" in result["error"]

    @pytest.mark.asyncio
    async def test_file_read_command_allowed_with_file_read(self, tmp_path):
        agent = _register(tmp_path, granted_permissions={Permission.FILE_READ})
        result = await _call(agent, command="ls")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_network_write_implication_denies_network_write_only_grant(self, tmp_path):
        """curl -d needs NETWORK_WRITE, which expand() also requires
        NETWORK_READ for — a caller missing NETWORK_READ is still denied
        even though NETWORK_WRITE alone was granted."""
        agent = _register(tmp_path, granted_permissions={Permission.NETWORK_WRITE})
        result = await _call(agent, command="curl -d x https://example.com")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_denied_call_never_reaches_dispatch(self, tmp_path, monkeypatch):
        """A capability denial must short-circuit before the shape/dispatch
        pipeline runs at all — not merely produce a 'Blocked:' shape message."""
        agent = _register(tmp_path, granted_permissions=set())
        called = []
        # Patch the module-level _run_local so we'd notice if dispatch ran.
        monkeypatch.setattr(shell_mod, "_run_local", lambda *a, **k: called.append(1) or "should not run")
        result = await _call(agent, command="rm -rf /nonexistent")  # FILE_WRITE-gated
        assert result["success"] is False
        assert not called


# ---------------------------------------------------------------------------
# backend_access -> BACKEND_EXEC (location permission, §5's "no longer a
# scalar comparison")
# ---------------------------------------------------------------------------

class TestBackendAccess:
    @pytest.mark.asyncio
    async def test_backend_access_denied_without_capability(self, tmp_path):
        agent = _register(tmp_path, granted_permissions=set())  # holds nothing
        result = await _call(agent, command="echo hi", backend_access=True)
        assert result["success"] is False
        assert "backend_exec" in result["error"]

    @pytest.mark.asyncio
    async def test_backend_access_allowed_with_capability(self, tmp_path):
        agent = _register(
            tmp_path, granted_permissions={Permission.BACKEND_EXEC},
        )
        result = await _call(agent, command="echo backend-ok", backend_access=True)
        assert result["success"] is True
        assert "backend-ok" in result["result"]

    @pytest.mark.asyncio
    async def test_backend_access_false_does_not_need_the_capability(self, tmp_path):
        agent = _register(tmp_path, granted_permissions=set())
        result = await _call(agent, command="echo hi", backend_access=False)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_backend_access_still_shape_checked(self, tmp_path):
        """Holding BACKEND_EXEC does not also grant UNTRUSTED_EXEC — a
        construct denial (command substitution) is still enforced, now via
        the same capability gate as everything else, not a separate check."""
        agent = _register(
            tmp_path,
            granted_permissions={Permission.BACKEND_EXEC},
            extra_shell={},
        )
        # Command substitution requires UNTRUSTED_EXEC, which this caller
        # doesn't hold — denied before shell() ever runs.
        result = await _call(agent, command="echo $(id)", backend_access=True)
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]


# ---------------------------------------------------------------------------
# Shape checking — construct denial folded into required_permissions_for_shell
# as an UNTRUSTED_EXEC requirement (§5.2), not a separate post-capability gate.
# ---------------------------------------------------------------------------

class TestShapeChecking:
    @pytest.mark.asyncio
    async def test_ordinary_command_passes_shape_check(self, tmp_path):
        agent = _register(tmp_path)  # default caller holds every Permission
        result = await _call(agent, command="echo neutral-ok")
        assert "Blocked" not in result["result"]
        assert "neutral-ok" in result["result"]

    @pytest.mark.asyncio
    async def test_pipeline_and_chaining_pass_shape_check(self, tmp_path):
        agent = _register(tmp_path)
        result = await _call(agent, command="echo hi; echo there")
        assert "Blocked" not in result["result"]

    @pytest.mark.asyncio
    async def test_command_substitution_denied_without_untrusted_exec(self, tmp_path):
        """$() requires UNTRUSTED_EXEC — a caller without it is denied
        before shell() ever runs, same seam as any other worst-cased
        command, not a separate 'Blocked:' shape message."""
        agent = _register(tmp_path, granted_permissions=set())
        result = await _call(agent, command="echo $(id)")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    @pytest.mark.asyncio
    async def test_command_substitution_allowed_with_untrusted_exec(self, tmp_path):
        """A caller who holds UNTRUSTED_EXEC is trusted to run syntax that
        can't be statically verified safe — the construct actually runs,
        it isn't hard-blocked regardless of capability."""
        agent = _register(tmp_path, granted_permissions={Permission.UNTRUSTED_EXEC})
        result = await _call(agent, command="echo $(echo nested)")
        assert result["success"] is True
        assert "Blocked" not in result["result"]
        assert "nested" in result["result"]

    @pytest.mark.asyncio
    async def test_quoted_metacharacters_are_data_not_shape_violations(self, tmp_path):
        agent = _register(tmp_path)
        result = await _call(agent, command='echo "it is 5pm; all fine, right?"')
        assert "Blocked" not in result["result"]
        assert "all fine" in result["result"]


# ---------------------------------------------------------------------------
# Fail-closed shape-policy loading
# ---------------------------------------------------------------------------

class TestShapePolicyLoadFailure:
    @pytest.mark.asyncio
    async def test_missing_builtin_allow_blocks_everything(self, tmp_path, monkeypatch):
        """If builtin:allow (the source of the shape policy's constructs
        map) can't load, every command must be blocked — never silently
        unrestricted, the opposite of the old blacklist.txt's failure mode."""
        import TinyCTX.modules.shell.policy as policy_mod_ref

        def _raise(*a, **k):
            raise policy_mod_ref.PolicyError("simulated load failure")

        monkeypatch.setattr(policy_mod_ref, "load_policy", _raise)
        agent = _register(tmp_path)  # unrestricted caller — must still be blocked
        result = await _call(agent, command="echo hi")
        assert result["success"] is True
        assert "Blocked" in result["result"]
        assert "could not be loaded" in result["result"]


# ---------------------------------------------------------------------------
# Sandbox vs local dispatch (unaffected by the permissions rework — smoke
# test that register_agent still wires dispatch correctly)
# ---------------------------------------------------------------------------

class TestDispatchRouting:
    @pytest.mark.asyncio
    async def test_no_sandbox_url_runs_locally(self, tmp_path):
        agent = _register(tmp_path, sandbox_url=None)
        result = await _call(agent, command="echo local-ok")
        assert "local-ok" in result["result"]

    @pytest.mark.asyncio
    async def test_backend_access_forces_local_even_with_sandbox_configured(self, tmp_path):
        agent = _register(
            tmp_path, granted_permissions=frozenset(Permission),
            sandbox_url="http://unreachable-sandbox-host:9999",
        )
        result = await _call(agent, command="echo local-override", backend_access=True)
        # backend_access routes to _run_local regardless of sandbox_url —
        # if it tried the sandbox URL instead this would fail/timeout.
        assert "local-override" in result["result"]


# ---------------------------------------------------------------------------
# Default registration (must not raise — shipped shape policy loads)
# ---------------------------------------------------------------------------

class TestDefaultRegistration:
    def test_registers_without_raising(self, tmp_path):
        agent = _register(tmp_path)
        assert "shell" in agent.tool_handler.tools

    def test_registered_with_dynamic_required_permissions_callable(self, tmp_path):
        agent = _register(tmp_path)
        assert agent.tool_handler.tools["shell"]["required_permissions"] is not None
        assert agent.tool_handler.tools["shell"]["static_permissions"] is None  # it's callable, not static


# ---------------------------------------------------------------------------
# Exit-code annotation (unaffected by the permissions rework)
# ---------------------------------------------------------------------------

class TestExitAnnotation:
    def test_last_command_of_a_pipeline_wins(self):
        assert shell_mod.validate.last_command_name("find . -name x | head") == "head"

    def test_pipe_inside_a_quoted_argument_is_not_a_pipe(self):
        assert shell_mod.validate.last_command_name('grep "a | b" file') == "grep"

    def test_grep_exit_1_is_not_an_error(self):
        assert shell_mod._annotate_exit("grep foo file", 1) == "(no matches found)"

    def test_grep_exit_2_is_an_error(self):
        assert shell_mod._annotate_exit("grep foo file", 2) == "(exit 2)"

    def test_unknown_command_falls_back_to_exit_code(self):
        assert shell_mod._annotate_exit("mycmd", 3) == "(exit 3)"


# ---------------------------------------------------------------------------
# Config-dir path resolution (unaffected by the permissions rework — kept
# from the pre-rework file, still exercises real code)
# ---------------------------------------------------------------------------

class TestConfigDirResolution:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from pathlib import Path
        from TinyCTX.utils.instance import runtime_config_dir

        monkeypatch.setenv("TINYCTX_CONFIG_DIR_PATH", "/app/config")
        monkeypatch.setenv("TINYCTX_CONFIG_FILE", str(tmp_path / "config.yaml"))
        assert runtime_config_dir() == Path("/app/config")

    def test_falls_back_to_config_file_sibling(self, tmp_path, monkeypatch):
        from TinyCTX.utils.instance import runtime_config_dir

        monkeypatch.delenv("TINYCTX_CONFIG_DIR_PATH", raising=False)
        monkeypatch.setenv("TINYCTX_CONFIG_FILE", str(tmp_path / "config.yaml"))
        assert runtime_config_dir() == tmp_path / "config"

    def test_falls_back_to_workspace_sibling(self, tmp_path, monkeypatch):
        from TinyCTX.utils.instance import runtime_config_dir

        monkeypatch.delenv("TINYCTX_CONFIG_DIR_PATH", raising=False)
        monkeypatch.delenv("TINYCTX_CONFIG_FILE", raising=False)
        assert runtime_config_dir(tmp_path / "workspace") == tmp_path / "config"

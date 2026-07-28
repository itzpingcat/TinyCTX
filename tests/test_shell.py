"""
tests/test_shell.py

Tests for modules/shell — the shell tool's permission-tiered access.

Covers:
  - The backend_access permission-resolution bug fix. The old check read
    agent.config.permissions.level, an attribute PermissionsConfig never
    defines (it only has minimal_tokens: bool) — so backend_access=True
    either crashed or (depending on how the AttributeError surfaced)
    never actually gated anything on the real caller. The fix reads
    agent.caller.permission_level instead, mirroring modules/sysops's
    caller_level snapshot pattern.
  - The new whitelist feature: callers below "neutral" may only run
    commands matching whitelist.txt, matched by fullmatch (not substring,
    unlike the blacklist) so a "git status" entry can't also cover
    "git status; rm -rf /".
  - The new extra.shell.permissions config block (use_whitelist, neutral,
    bypass_blacklist, access_backend).

Uses lightweight fakes for agent/config/caller (mirrors tests/test_sysops.py's
style) and monkeypatches the blacklist/whitelist loaders to point at temp
files, so tests don't depend on the real blacklist.txt/whitelist.txt
contents drifting over time.

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.modules.shell import __main__ as shell_mod
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

def _isolate_lists(monkeypatch, tmp_path, blacklist_lines, whitelist_lines):
    """Point the module's blacklist/whitelist loaders at throwaway files so
    tests don't depend on (or corrupt) the real blacklist.txt/whitelist.txt."""
    bl_path = tmp_path / "blacklist.txt"
    bl_path.write_text("\n".join(blacklist_lines))
    wl_path = tmp_path / "whitelist.txt"
    wl_path.write_text("\n".join(whitelist_lines))

    real_load_blacklist = shell_mod._load_blacklist
    real_load_whitelist = shell_mod._load_whitelist
    monkeypatch.setattr(shell_mod, "_load_blacklist", lambda path=bl_path: real_load_blacklist(path))
    monkeypatch.setattr(shell_mod, "_load_whitelist", lambda path=wl_path: real_load_whitelist(path))


def _register(tmp_path, monkeypatch, caller_level, extra_shell=None,
              blacklist_lines=(), whitelist_lines=()):
    _isolate_lists(monkeypatch, tmp_path, blacklist_lines, whitelist_lines)
    extra = {"shell": {"sandbox_url": None, **(extra_shell or {})}}
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


# ---------------------------------------------------------------------------
# backend_access permission-resolution bug fix
# ---------------------------------------------------------------------------

class TestBackendAccessBugFix:
    @pytest.mark.asyncio
    async def test_backend_access_denied_below_threshold_does_not_crash(self, tmp_path, monkeypatch):
        # Old code read agent.config.permissions.level, which doesn't exist
        # -> would raise AttributeError instead of a clean denial.
        agent = _register(tmp_path, monkeypatch, caller_level=50)
        result = await _call(agent, command="pwd", backend_access=True)
        assert result["success"] is True
        assert "Blocked" in result["result"]
        assert "80" in result["result"]
        assert "50" in result["result"]

    @pytest.mark.asyncio
    async def test_backend_access_allowed_at_threshold(self, tmp_path, monkeypatch):
        agent = _register(tmp_path, monkeypatch, caller_level=80)
        result = await _call(agent, command="echo backend-ok", backend_access=True)
        assert result["success"] is True
        assert "Blocked" not in result["result"]
        assert "backend-ok" in result["result"]

    @pytest.mark.asyncio
    async def test_backend_access_threshold_is_configurable(self, tmp_path, monkeypatch):
        agent = _register(
            tmp_path, monkeypatch, caller_level=90,
            extra_shell={"permissions": {"access_backend": 95}},
        )
        result = await _call(agent, command="pwd", backend_access=True)
        assert "Blocked" in result["result"]
        assert "95" in result["result"]


# ---------------------------------------------------------------------------
# Whitelist gate for reduced-permission callers
# ---------------------------------------------------------------------------

class TestWhitelistGate:
    @pytest.mark.asyncio
    async def test_at_neutral_runs_arbitrary_commands(self, tmp_path, monkeypatch):
        agent = _register(tmp_path, monkeypatch, caller_level=45)  # default neutral
        result = await _call(agent, command="echo neutral-ok")
        assert "neutral-ok" in result["result"]

    @pytest.mark.asyncio
    async def test_below_neutral_blocked_without_whitelist_match(self, tmp_path, monkeypatch):
        agent = _register(
            tmp_path, monkeypatch, caller_level=20,
            whitelist_lines=["echo hi"],
        )
        result = await _call(agent, command="echo bye")
        assert result["success"] is True
        assert "Blocked" in result["result"]
        assert "whitelisted" in result["result"]

    @pytest.mark.asyncio
    async def test_below_neutral_allowed_with_whitelist_match(self, tmp_path, monkeypatch):
        agent = _register(
            tmp_path, monkeypatch, caller_level=20,
            whitelist_lines=["echo hi"],
        )
        result = await _call(agent, command="echo hi")
        assert "Blocked" not in result["result"]
        assert "hi" in result["result"]

    @pytest.mark.asyncio
    async def test_whitelist_match_is_fullmatch_not_substring(self, tmp_path, monkeypatch):
        # A literal "echo hi" entry must not also cover a longer command
        # that merely contains "echo hi" as a substring/prefix.
        agent = _register(
            tmp_path, monkeypatch, caller_level=20,
            whitelist_lines=["echo hi"],
        )
        result = await _call(agent, command="echo hi; echo pwned")
        assert "Blocked" in result["result"]

    @pytest.mark.asyncio
    async def test_below_use_whitelist_denied_at_framework_level(self, tmp_path, monkeypatch):
        agent = _register(tmp_path, monkeypatch, caller_level=5)  # below default use_whitelist=10
        result = await _call(agent, command="echo nope")
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    def test_registered_min_permission_matches_use_whitelist_config(self, tmp_path, monkeypatch):
        agent = _register(
            tmp_path, monkeypatch, caller_level=100,
            extra_shell={"permissions": {"use_whitelist": 33}},
        )
        assert agent.tool_handler.tools["shell"]["min_permission"] == 33


# ---------------------------------------------------------------------------
# {arg} placeholder — free-text whitelist arguments without injection
# ---------------------------------------------------------------------------

class TestArgPlaceholder:
    """Unit tests directly against _whitelist_glob_to_regex/_check_whitelist:
    faster and more precise than going through the full tool pipeline for
    checking exactly which strings the {arg} character class accepts."""

    def _patterns(self, *lines):
        return [shell_mod._whitelist_glob_to_regex(line) for line in lines]

    def test_plain_text_argument_matches(self):
        patterns = self._patterns('echo "{arg}"')
        assert shell_mod._check_whitelist('echo "cat"', patterns)

    def test_sentence_with_punctuation_and_apostrophe_matches(self):
        patterns = self._patterns('echo "{arg}"')
        assert shell_mod._check_whitelist('echo "it\'s a nice day, right?"', patterns)

    def test_trailing_chained_command_does_not_match(self):
        patterns = self._patterns('echo "{arg}"')
        assert not shell_mod._check_whitelist('echo "cat"; rm -rf /', patterns)

    def test_quote_breakout_attempt_does_not_match(self):
        patterns = self._patterns('echo "{arg}"')
        assert not shell_mod._check_whitelist('echo "cat" "; rm -rf /"', patterns)

    def test_command_substitution_characters_do_not_match(self):
        patterns = self._patterns('echo "{arg}"')
        assert not shell_mod._check_whitelist('echo "$(whoami)"', patterns)
        assert not shell_mod._check_whitelist('echo "`whoami`"', patterns)

    def test_pipe_and_semicolon_and_ampersand_do_not_match(self):
        patterns = self._patterns('echo "{arg}"')
        for injected in ('echo "cat" | mail x@y.com', 'echo "cat" & rm -rf /', 'echo "cat" > /etc/passwd'):
            assert not shell_mod._check_whitelist(injected, patterns)

    @pytest.mark.asyncio
    async def test_end_to_end_through_shell_tool(self, tmp_path, monkeypatch):
        agent = _register(
            tmp_path, monkeypatch, caller_level=20,
            whitelist_lines=['echo "{arg}"'],
        )
        ok = await _call(agent, command='echo "hello there"')
        assert "Blocked" not in ok["result"]
        assert "hello there" in ok["result"]

        blocked = await _call(agent, command='echo "hello"; rm -rf /tmp')
        assert "Blocked" in blocked["result"]


# ---------------------------------------------------------------------------
# The shipped whitelist.txt (real file, not a fixture)
# ---------------------------------------------------------------------------

class TestRealWhitelistFile:
    @pytest.mark.asyncio
    async def test_echo_arg_and_date_are_enabled_by_default(self, tmp_path, monkeypatch):
        # Only isolate the blacklist here — deliberately exercise the real
        # shipped whitelist.txt so a future edit that breaks it fails a test.
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("")
        real_load_blacklist = shell_mod._load_blacklist
        monkeypatch.setattr(shell_mod, "_load_blacklist", lambda path=bl_path: real_load_blacklist(path))

        extra = {"shell": {"sandbox_url": None}}
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        config = _FakeConfig(workspace, extra=extra)
        caller = _FakeCaller(20)  # between default use_whitelist=10 and neutral=45
        agent = _FakeAgent(caller, config)
        shell_mod.register_agent(agent)

        echo_result = await _call(agent, command='echo "public status ok"')
        assert "Blocked" not in echo_result["result"]
        assert "public status ok" in echo_result["result"]

        date_result = await _call(agent, command="date")
        assert "Blocked" not in date_result["result"]

        for cmd in ("ps", "ps -eo pid,comm"):
            ps_result = await _call(agent, command=cmd)
            assert "Blocked" not in ps_result["result"], cmd

        cal_result = await _call(agent, command="cal")
        assert "Blocked" in cal_result["result"]  # commented out, not installed everywhere

        # Never whitelisted: these show full ARGV (and, for -e, environment)
        # of whatever else is running in the shared sandbox container.
        for leaky in ("ps aux", "ps -ef", "ps -eo pid,args", "ps e"):
            leak_result = await _call(agent, command=leaky)
            assert "Blocked" in leak_result["result"], leaky


# ---------------------------------------------------------------------------
# Blacklist + bypass_blacklist
# ---------------------------------------------------------------------------

class TestBlacklistAndBypass:
    @pytest.mark.asyncio
    async def test_blacklist_blocks_matching_command(self, tmp_path, monkeypatch):
        agent = _register(
            tmp_path, monkeypatch, caller_level=89,  # below default bypass_blacklist=90
            blacklist_lines=["*dangerous-marker*"],
        )
        result = await _call(agent, command="echo dangerous-marker")
        assert "Blocked" in result["result"]
        assert "blacklist pattern" in result["result"]

    @pytest.mark.asyncio
    async def test_bypass_blacklist_skips_check_at_threshold(self, tmp_path, monkeypatch):
        agent = _register(
            tmp_path, monkeypatch, caller_level=90,  # default bypass_blacklist
            blacklist_lines=["*dangerous-marker*"],
        )
        result = await _call(agent, command="echo dangerous-marker")
        assert "Blocked" not in result["result"]
        assert "dangerous-marker" in result["result"]


# ---------------------------------------------------------------------------
# Default permission config
# ---------------------------------------------------------------------------

class TestDefaultPermissions:
    def test_defaults_applied_when_unconfigured(self, tmp_path, monkeypatch):
        agent = _register(tmp_path, monkeypatch, caller_level=100)
        assert agent.tool_handler.tools["shell"]["min_permission"] == 10

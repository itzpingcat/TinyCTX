"""
modules/shell/__main__.py

Registers the `shell` tool into the agent's tool_handler.

Execution modes:
  SANDBOX (sandbox_url configured)
    POSTs {"command": "..."} to the sandbox HTTP service over the internal
    Docker network (agent_sandbox). The sandbox container has no route to
    the host LAN or Tailscale — network isolation is enforced at the compose
    level, not in code. No auth token needed: the sandbox port is only
    reachable from the agent container by design.

  LOCAL (sandbox_url not set)
    Runs via `bash -c` in the main container. Used for backend_access=True and
    for bare-metal installs. Linux only — PowerShell support was removed with
    the container refactor.

Two independent checks run before any dispatch, in both modes. The sandbox
itself runs whatever it receives — it trusts the agent.

  1. CAPABILITY — "is this caller permitted to do this at all". Per-command
     tagging in perms.py classifies each resolved command into the
     TinyCTX.permissions.Permission bools it needs (FILE_WRITE,
     NETWORK_WRITE, UNTRUSTED_EXEC, ...); shell's `required_permissions`
     callable (registered below) is checked once, centrally, by
     tool_handling.handler.ToolCallHandler — same seam every other tool
     uses. See docs/PERMISSIONS-PLAN.md §5.

  2. SHAPE — "is this invocation syntactically safe, independent of who's
     asking". `_dispatch` below still runs validate.check() against a
     single always-applied policy built from allow.yaml's `constructs` map,
     which is what makes injection structurally impossible (see
     validate.py's module docstring) — the `constructs` allow-map rejects
     `$()`, unquoted globs used as commands, and control-flow constructs a
     tree-sitter-bash grammar upgrade might introduce, BEFORE any command
     is even classified. This is a different, complementary concern from
     capability checking (§5.2) — retired is the old per-command
     allow/deny RULE matching that used to stand in for capability
     declarations (`applies_below` tier selection); retained is the
     construct/shape check underneath it.

Command policy — both checks — is AST-based: the command is parsed with
tree-sitter-bash and each resolved command in it is checked separately (see
validate.py). This replaced substring-glob blacklist.txt / whitelist.txt
files, which could not tell a command from a quoted argument that happened
to contain the same text.

`backend_access=True` no longer compares against a scalar level — it adds
Permission.BACKEND_EXEC to what the classifier requires (perms.py), enforced
by the same central seam as everything else. See permissions.py's
BACKEND_EXEC docstring for why this is a *location* permission (which
container the command runs in), not a capability about what the command
itself does.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from TinyCTX.utils.instance import runtime_config_dir

from . import perms as shell_perms
from . import policy as policy_mod
from . import validate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exit-code interpretation
# ---------------------------------------------------------------------------

_EXIT_SEMANTICS: dict[str, Callable[[int], tuple[bool, str | None]]] = {
    "grep":  lambda c: (c >= 2, "no matches found" if c == 1 else None),
    "rg":    lambda c: (c >= 2, "no matches found" if c == 1 else None),
    "egrep": lambda c: (c >= 2, "no matches found" if c == 1 else None),
    "fgrep": lambda c: (c >= 2, "no matches found" if c == 1 else None),
    "diff":  lambda c: (c >= 2, "files differ" if c == 1 else None),
    "test":  lambda c: (c >= 2, "condition is false" if c == 1 else None),
    "[":     lambda c: (c >= 2, "condition is false" if c == 1 else None),
    "find":  lambda c: (c >= 2, "some directories were inaccessible" if c == 1 else None),
}


def _annotate_exit(command: str, code: int) -> str:
    if code == 0:
        return ""
    sem = _EXIT_SEMANTICS.get(validate.last_command_name(command))
    if sem:
        is_err, msg = sem(code)
        if not is_err:
            return f"({msg})" if msg else ""
    return f"(exit {code})"


# ---------------------------------------------------------------------------
# Safe env for local subprocess
# ---------------------------------------------------------------------------

_SAFE_KEYS = (
    "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    "TERM", "USER", "LOGNAME",
)
_LOCAL_ENV = {k: v for k, v in os.environ.items() if k in _SAFE_KEYS}


# ---------------------------------------------------------------------------
# Dispatch: sandbox HTTP
# ---------------------------------------------------------------------------

def _run_sandbox(command: str, sandbox_url: str, timeout: int) -> str:
    endpoint = sandbox_url.rstrip("/") + "/exec"
    payload = json.dumps({"command": command}).encode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            body = json.loads(resp.read().decode())
            return body.get("output", "Error: sandbox returned no output field")
    except urllib.error.URLError as exc:
        return f"Error: cannot reach sandbox at {sandbox_url} — {exc.reason}"
    except Exception as exc:
        return f"Error: sandbox request failed — {exc}"


# ---------------------------------------------------------------------------
# Dispatch: local
# ---------------------------------------------------------------------------

def _run_local(command: str, cwd: Path, timeout: int) -> str:
    try:
        result = subprocess.run(
            ["bash", "-c", command], cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            env=_LOCAL_ENV,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"stderr:\n{result.stderr.rstrip()}")
        annotation = _annotate_exit(command, result.returncode)
        if annotation:
            parts.append(annotation)
        return "\n".join(parts) if parts else "No output"
    except subprocess.TimeoutExpired:
        return f"Error: timed out after {timeout}s"
    except FileNotFoundError as exc:
        return f"Error: shell not found — {exc}"
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Module registration
# ---------------------------------------------------------------------------

def register_agent(agent) -> None:
    workspace = Path(agent.config.workspace.path).expanduser().resolve()

    _extra      = agent.config.extra.get("shell", {}) if hasattr(agent.config, "extra") else {}
    default_timeout = int(_extra.get("default_timeout", 120))
    max_timeout     = int(_extra.get("max_timeout", 1200))
    # Default to sandbox. Operators set null explicitly for bare-metal / dev.
    # The sandbox container name is TINYCTX_INSTANCE (hashed per-instance, see
    # utils/instance.py::project_name_for) + "_sandbox" — falls back to
    # "tinyctx" to match compose.yaml's own default when unset.
    _default_sandbox_url = f"http://{os.environ.get('TINYCTX_INSTANCE', 'tinyctx')}_sandbox:8700"
    sandbox_url = _extra.get("sandbox_url", _default_sandbox_url) or None

    if sandbox_url:
        logger.info("shell: dispatching via sandbox at %s", sandbox_url)
    else:
        logger.info("shell: dispatching locally (no sandbox configured)")

    # SHAPE policy only — construct/redirect/glob-shape validation that runs
    # regardless of who's calling (§5.2). Built from builtin:allow's
    # `constructs` map (the strictest available — it's what makes injection
    # structurally impossible, see validate.py) but with default_action
    # forced to "allow" and no rules: the per-command allow/deny RULES that
    # used to stand in for capability decisions are retired — perms.py's
    # classify() + the central ToolCallHandler seam owns that now (§5, §5.2).
    #
    # Fail closed. A policy that won't load blocks every command — it must
    # never degrade into an unrestricted shell, which is what the old
    # blacklist.txt did when its file was missing.
    config_dir = runtime_config_dir(workspace)
    policy_error: str | None = None
    shape_policy = None
    try:
        _base = policy_mod.load_policy(policy_mod.ALLOW_PATH, workspace)
        shape_policy = policy_mod.Policy(
            name="shape-only (derived from builtin:allow constructs)",
            default_action="allow",
            constructs=_base.constructs,
            rules=(),
            max_command_bytes=_base.max_command_bytes,
        )
    except policy_mod.PolicyError as exc:
        policy_error = str(exc)
        logger.error("shell: shape policy failed to load — all commands blocked: %s", exc)

    def _dispatch(command: str, local: bool = False, call_timeout: int | None = None) -> str:
        """Shared pipeline: shape-validate, then dispatch. Capability
        checking already happened before this function is ever reached — see
        the required_permissions=shell_perms.required_permissions_for_shell
        registration below, enforced by ToolCallHandler."""
        if policy_error is not None:
            return f"Blocked: shell policy could not be loaded — {policy_error}"

        verdict = validate.check(command, shape_policy, workspace)
        if not verdict.allowed:
            logger.warning("shell: blocked by shape check — %s", verdict.reason)
            return f"Blocked: {verdict.reason}."
        prefix = "\n".join(dict.fromkeys(verdict.warnings)) + "\n" if verdict.warnings else ""

        effective_timeout = min(call_timeout, max_timeout) if call_timeout is not None else default_timeout
        if local or not sandbox_url:
            output = _run_local(command, workspace, effective_timeout)
        else:
            output = _run_sandbox(command, sandbox_url, effective_timeout)
        return prefix + output

    def shell(command: str, timeout: int | None = None, backend_access: bool = False) -> str:
        # No hand-rolled backend_access gate here anymore — perms.py's
        # required_permissions_for_shell adds Permission.BACKEND_EXEC to what
        # this call needs, and ToolCallHandler.execute_tool_call already
        # denied the call before shell() ever ran if the caller lacks it.
        return _dispatch(command, local=backend_access, call_timeout=timeout)

    shell.__doc__ = """Run a shell command.

        By default runs in the isolated sandbox container, which has outbound
        internet access (HTTP/S, git, pip, npm, etc.) but is NETWORK-ISOLATED:
        it cannot reach the local LAN, Tailscale peers, or internal services.
        Use this for the vast majority of shell work.

        Set backend_access=True to run in the main TinyCTX container instead,
        which has full network access and its own backend files — use when
        the command needs to reach a private or local address, e.g.:
          - Tailscale IPs (100.x.x.x)
          - LAN services (192.168.x.x, 10.x.x.x)
          - Internal APIs (ComfyUI, local databases, self-hosted services)
          - Docker host or sibling containers by hostname
        Requires the backend_exec capability. Command policy still applies in
        both modes.

        The command is parsed as bash and each command in it is checked
        separately, so chaining with ; && || and pipes is fine and quoted
        arguments are treated as data. Blocked commands say which rule
        objected. Which specific commands you may run depends on your
        granted capabilities (file_read, file_write, network_read,
        network_write, untrusted_exec, ...) — most everyday commands
        (cat, ls, grep, curl, git clone, ...) need only a narrow one or two
        of these; anything unrecognized requires untrusted_exec.

        Args:
            command: The shell command to run.
            timeout: Optional per-call timeout in seconds. Capped at the
                     configured maximum (default {max_timeout}s).
            backend_access: If True, run in the main container with full
                            network access and access to its own backend
                            files (requires the backend_exec capability).
        """.format(max_timeout=max_timeout)

    # docs/PERMISSIONS-PLAN.md §5 — dynamic classifier, third alongside
    # `present` (§7.1). listing_permissions is deliberately left unset
    # (empty) — under minimal_tokens, any caller might be permitted to run
    # *some* command, so hiding the tool entirely would be wrong (§3.2).
    agent.tool_handler.register_tool(
        shell, always_on=True,
        required_permissions=shell_perms.required_permissions_for_shell,
    )

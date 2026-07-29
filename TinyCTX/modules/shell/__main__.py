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

Command policy is enforced here, before any dispatch, in both modes. The
sandbox itself runs whatever it receives — it trusts the agent.

Policy is AST-based: the command is parsed with tree-sitter-bash and each
resolved command in it is checked separately (see validate.py, and PLAN.md for
the design). This replaced substring-glob blacklist.txt / whitelist.txt files,
which could not tell a command from a quoted argument that happened to contain
the same text.

Which policies apply to whom is config, not code. Each policy carries the level
at which a caller outgrows it:

    min_permission: 30
    policies:
      - {policy: builtin:allow, applies_below: 45}
      - {policy: builtin:deny,  applies_below: 90}

Level 20 is subject to both, 50 to the deny-list only, 95 to nothing. Omit
`applies_below` for a policy that binds everyone. `min_permission` is the level
below which the tool isn't offered at all.

Tier selection is therefore one comparison per policy, and this module has no
opinion on how many tiers exist or which policy is an allow-list vs a
deny-list — each file declares its own posture via `default_action`. Note what
the shape rules out: a policy cannot bind a HIGHER-privileged caller while
sparing a lower one. That would be incoherent, and a flat threshold list makes
it unrepresentable.

A relative `policy:` name resolves against the instance's config directory
(`<instance>/config`, bound read-only at /app/config in the container), so the
same config.yaml works on the host and in Docker where that directory has a
different absolute path.

`extra.shell.permissions.access_backend` remains a scalar: it gates *where* a
command runs, not which policy applies to it.

Levels are resolved from the *actual caller* (agent.caller.permission_level),
captured once per cycle — never from a static config value.
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

from . import policy as policy_mod
from . import validate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permission tiers
# ---------------------------------------------------------------------------

# Fallbacks when extra.shell is unset. Kept in sync with __init__.py's
# EXTENSION_META default_config, which is what an operator actually sees.
_DEFAULT_MIN_PERMISSION = 30
_DEFAULT_POLICIES = [
    {"policy": "builtin:allow", "applies_below": 45},
    {"policy": "builtin:deny", "applies_below": 90},
]


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

    _perm_raw = _extra.get("permissions", {}) or {}
    _removed = {"use_whitelist", "neutral", "bypass_blacklist"} & set(_perm_raw)
    access_backend = int(_perm_raw.get("access_backend", 80))

    if sandbox_url:
        logger.info("shell: dispatching via sandbox at %s", sandbox_url)
    else:
        logger.info("shell: dispatching locally (no sandbox configured)")

    # Which policies apply at which permission level is config, not code.
    # Relative policy names resolve against the instance's config directory,
    # which is at a different absolute path inside the container than on the
    # host — so `policy: shell-allow.yaml` works in both. Files are read-only
    # by design (compose binds /app/config read-only) and cached by path.
    #
    # Fail closed. A policy that won't load blocks every command — it must
    # never degrade into an unrestricted shell, which is what the old
    # blacklist.txt did when its file was missing.
    min_permission = int(_extra.get("min_permission", _DEFAULT_MIN_PERMISSION))
    config_dir = runtime_config_dir(workspace)

    policy_error: str | None = None
    policies = None
    if _removed:
        # Silently ignoring these would be a security bug in the loosening
        # direction: someone who set use_whitelist: 90 to lock the shell down
        # would get the permissive default instead.
        policy_error = (
            f"extra.shell.permissions no longer accepts {sorted(_removed)} — "
            "these were replaced by extra.shell.min_permission and the "
            "extra.shell.policies list (see modules/shell/__init__.py). "
            "Migrate the config to re-enable the shell."
        )
        logger.error("shell: %s", policy_error)
    else:
        try:
            policies = policy_mod.load_policy_set(
                _extra.get("policies") or _DEFAULT_POLICIES, workspace, config_dir,
            )
        except policy_mod.PolicyError as exc:
            policy_error = str(exc)
            logger.error("shell: policies failed to load — all commands blocked: %s", exc)

    # Caller's permission level for this cycle, resolved once. Mirrors
    # modules/sysops/__main__.py's caller_level snapshot: agent.caller is
    # set before register_agent runs and never changes mid-cycle, so this
    # closure-captured int always reflects the actual caller.
    caller_level = agent.caller.permission_level

    def _dispatch(command: str, local: bool = False, call_timeout: int | None = None) -> str:
        """Shared pipeline: apply every policy this caller is subject to, then
        dispatch.

        Note what this doesn't know: which policy is an allow-list and which is
        a deny-list, or how many tiers exist. Each file declares its own
        posture and carries its own threshold, so tier selection is one
        comparison per policy.
        """
        if policy_error is not None:
            return f"Blocked: shell policy could not be loaded — {policy_error}"

        warnings: list[str] = []
        for policy in policies.for_level(caller_level):
            verdict = validate.check(command, policy, workspace)
            if not verdict.allowed:
                logger.warning("shell: blocked by %s — %s", policy.name, verdict.reason)
                return f"Blocked: {verdict.reason}. Your permission level is {caller_level}."
            warnings.extend(verdict.warnings)
        prefix = "\n".join(dict.fromkeys(warnings)) + "\n" if warnings else ""

        effective_timeout = min(call_timeout, max_timeout) if call_timeout is not None else default_timeout
        if local or not sandbox_url:
            output = _run_local(command, workspace, effective_timeout)
        else:
            output = _run_sandbox(command, sandbox_url, effective_timeout)
        return prefix + output

    def shell(command: str, timeout: int | None = None, backend_access: bool = False) -> str:
        if backend_access:
            if caller_level < access_backend:
                return (
                    f"Blocked: backend_access=True requires permission level "
                    f"{access_backend} (yours is {caller_level})."
                )
            return _dispatch(command, local=True, call_timeout=timeout)
        return _dispatch(command, call_timeout=timeout)

    shell.__doc__ = f"""Run a shell command.

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
        Requires permission level {access_backend}. Command policy still applies in both modes.

        The command is parsed as bash and each command in it is checked
        separately, so chaining with ; && || and pipes is fine and quoted
        arguments are treated as data. Blocked commands say which rule
        objected. Which policies apply depends on your permission level; at
        lower levels only an explicitly allow-listed set of commands runs.

        Args:
            command: The shell command to run.
            timeout: Optional per-call timeout in seconds. Capped at the
                     configured maximum (default {max_timeout}s).
            backend_access: If True, run in the main container with full
                            network access and access to its own backend
                            files (requires permission level {access_backend}).
        """

    agent.tool_handler.register_tool(shell, always_on=True, min_permission=min_permission)

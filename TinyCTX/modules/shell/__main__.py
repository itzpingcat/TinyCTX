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
    Runs via bash (Linux/macOS) or PowerShell (Windows) directly.
    Used for bare-metal installs and local dev.

The blacklist is enforced here, before any dispatch, in both modes.
The sandbox itself runs whatever it receives — it trusts the agent.

Permission tiers (see `_ShellPermissions` below, configured via
extra.shell.permissions in config.yaml):
  use_whitelist     — min caller level to invoke the tool at all. Below
                       `neutral`, every command must match whitelist.txt.
  neutral           — min caller level for unrestricted commands (still
                       subject to the blacklist unless bypass_blacklist).
  bypass_blacklist  — min caller level that skips the blacklist check.
  access_backend    — min caller level for backend_access=True.
All four are resolved from the *actual caller* (agent.caller.permission_level),
captured once per cycle — never from a static config value.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

_BLACKLIST_PATH = Path(__file__).parent / "blacklist.txt"
_WHITELIST_PATH = Path(__file__).parent / "whitelist.txt"


# ---------------------------------------------------------------------------
# Permission tiers
# ---------------------------------------------------------------------------

class _ShellPermissions(NamedTuple):
    use_whitelist: int
    neutral: int
    bypass_blacklist: int
    access_backend: int


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> re.Pattern:
    # Translate glob wildcards to regex BEFORE escaping, so backslashes
    # in patterns like *\\windows\\*\** don't interfere with wildcard
    # expansion. Each literal character is escaped individually.
    parts = []
    for ch in pattern:
        if ch == '*':
            parts.append('.*')
        elif ch == '?':
            parts.append('.')
        else:
            parts.append(re.escape(ch))
    return re.compile(''.join(parts), re.IGNORECASE)


def _load_blacklist(path: Path = _BLACKLIST_PATH) -> list[re.Pattern]:
    if not path.exists():
        logger.warning("shell: blacklist not found at %s — shell is unrestricted", path)
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(_glob_to_regex(line))
    logger.debug("shell: loaded %d blacklist patterns", len(patterns))
    return patterns


def _check_blacklist(command: str, patterns: list[re.Pattern]) -> str | None:
    normalized = command.strip().lower()
    for p in patterns:
        if p.search(normalized):
            return p.pattern
    return None


# ---------------------------------------------------------------------------
# Whitelist (reduced-permission commands — see permissions.use_whitelist)
# ---------------------------------------------------------------------------

# Placeholder for a free-text argument in a whitelist pattern, e.g.
# `echo "{arg}"`. Expands to a restricted, allowlisted character class —
# NOT ".*" like `*` does — so it can hold arbitrary natural-language text
# without ever containing a shell metacharacter. Letters, digits, spaces,
# and a small set of punctuation for normal sentences; notably excludes
# ; & | ` $ ( ) < > " \ * ? [ ] { } ~ # = and newlines, so nothing captured
# by {arg} can terminate the intended command or start a new one.
_ARG_TOKEN = "{arg}"
_ARG_CLASS = r"[A-Za-z0-9 ,.!?:'-]*"


def _whitelist_glob_to_regex(pattern: str) -> re.Pattern:
    """Like `_glob_to_regex`, plus support for the `{arg}` placeholder.

    `*`/`?` behave exactly as in the blacklist (`*` -> match-anything,
    including shell metacharacters) and remain available for matching
    fixed command families, but are unsafe for capturing free text a
    reduced-permission caller controls — use `{arg}` for that instead.

    `{arg}` is only injection-safe when wrapped in double quotes in the
    pattern (e.g. `echo "{arg}"`): the class excludes `"`, so the match
    can't contain a stray closing quote, and it excludes every shell
    metacharacter, so nothing inside can end the command or chain another
    one. An unquoted `{arg}` risks a bash syntax error (not injection —
    just a broken command) if the text contains an apostrophe.
    """
    parts = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith(_ARG_TOKEN, i):
            parts.append(_ARG_CLASS)
            i += len(_ARG_TOKEN)
            continue
        ch = pattern[i]
        if ch == '*':
            parts.append('.*')
        elif ch == '?':
            parts.append('.')
        else:
            parts.append(re.escape(ch))
        i += 1
    return re.compile(''.join(parts), re.IGNORECASE)


def _load_whitelist(path: Path = _WHITELIST_PATH) -> list[re.Pattern]:
    if not path.exists():
        logger.debug("shell: whitelist not found at %s — no reduced-permission commands available", path)
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(_whitelist_glob_to_regex(line))
    logger.debug("shell: loaded %d whitelist patterns", len(patterns))
    return patterns


def _check_whitelist(command: str, patterns: list[re.Pattern]) -> bool:
    """True if `command` is fully covered by a whitelist entry.

    Unlike the blacklist (which searches for a dangerous substring anywhere
    in the command), a whitelist entry must match the ENTIRE command. A
    substring match would let e.g. a `git status` entry also pass
    `git status; rm -rf /` — anchoring on the full string closes that.
    """
    normalized = command.strip().lower()
    return any(p.fullmatch(normalized) for p in patterns)


# ---------------------------------------------------------------------------
# Destructive command warnings (soft — prepended to output, not blocked)
# ---------------------------------------------------------------------------

_DESTRUCTIVE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bgit\s+reset\s+--hard\b"),                                          "warning: may discard uncommitted changes"),
    (re.compile(r"\bgit\s+push\b[^;&|\n]*\s+(--force|--force-with-lease|-f)\b"),       "warning: may overwrite remote history"),
    (re.compile(r"\bgit\s+clean\b(?![^;&|\n]*(?:-[a-zA-Z]*n|--dry-run))[^;&|\n]*-[a-zA-Z]*f"), "warning: may permanently delete untracked files"),
    (re.compile(r"\bgit\s+checkout\s+(--\s+)?\.[ \t]*($|[;&|\n])"),                   "warning: may discard all working tree changes"),
    (re.compile(r"\bgit\s+restore\s+(--\s+)?\.[ \t]*($|[;&|\n])"),                    "warning: may discard all working tree changes"),
    (re.compile(r"\bgit\s+stash\s+(drop|clear)\b"),                                    "warning: may permanently remove stashed changes"),
    (re.compile(r"\bgit\s+branch\s+(-D\s|--delete\s+--force|--force\s+--delete)\b"),  "warning: may force-delete a branch"),
    (re.compile(r"\bgit\s+(commit|push|merge)\b[^;&|\n]*--no-verify\b"),               "warning: skipping safety hooks"),
    (re.compile(r"\bgit\s+commit\b[^;&|\n]*--amend\b"),                                "warning: rewriting the last commit"),
    (re.compile(r"(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*[rR][a-zA-Z]*f|(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*f[a-zA-Z]*[rR]"), "warning: recursively force-removing files"),
    (re.compile(r"(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*[rR]"),                                "warning: recursively removing files"),
    (re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),      "warning: dropping/truncating database objects"),
    (re.compile(r"\bDELETE\s+FROM\s+\w+\s*(;|\"|\n|$)", re.IGNORECASE),               "warning: deleting all rows from a table"),
    (re.compile(r"\bkubectl\s+delete\b"),                                               "warning: deleting Kubernetes resources"),
    (re.compile(r"\bterraform\s+destroy\b"),                                            "warning: destroying Terraform infrastructure"),
]


def _destructive_warning(command: str) -> str | None:
    for pattern, msg in _DESTRUCTIVE:
        if pattern.search(command):
            return msg
    return None


# ---------------------------------------------------------------------------
# Exit-code interpretation
# ---------------------------------------------------------------------------

def _last_cmd(command: str) -> str:
    segments = re.split(r"\|", command)
    last = segments[-1].strip() if segments else command.strip()
    for token in last.split():
        if "=" in token and not token.startswith("-"):
            continue
        return token.split("/")[-1]
    return ""


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
    sem = _EXIT_SEMANTICS.get(_last_cmd(command))
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
    "SystemRoot", "SystemDrive", "windir",
    "PATHEXT", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
)
_LOCAL_ENV = {k: v for k, v in os.environ.items() if k in _SAFE_KEYS}


# ---------------------------------------------------------------------------
# Windows: normalize common Unix read commands to PowerShell equivalents
# ---------------------------------------------------------------------------

def _normalize_windows(command: str) -> str:
    if not _IS_WINDOWS:
        return command
    stripped = command.strip()
    if not stripped or any(c in stripped for c in ("|", ";", "&", "\n", "\r")):
        return command
    try:
        tokens = shlex.split(stripped, posix=False)
    except ValueError:
        return command
    if not tokens:
        return command
    cmd = tokens[0].lower()
    if cmd == "pwd" and len(tokens) == 1:
        return "Get-Location"
    if cmd not in {"ls", "ll"}:
        return command
    flags: set[str] = set()
    paths: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-") and not paths:
            chars = set(token[1:].lower())
            if not chars.issubset({"a", "l"}):
                return command
            flags.update(chars)
        else:
            paths.append(token)
    parts = ["Get-ChildItem"]
    if "a" in flags:
        parts.append("-Force")
    if paths:
        quoted = ", ".join("'" + p.replace("'", "''") + "'" for p in paths)
        parts.append(f"-LiteralPath {quoted}")
    return " ".join(parts)


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
    effective = _normalize_windows(command)
    if _IS_WINDOWS:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", effective]
    else:
        args = ["bash", "-c", command]
    try:
        if _IS_WINDOWS:
            result = subprocess.run(
                args, cwd=cwd,
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
                env=_LOCAL_ENV, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                args, cwd=cwd,
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
    permissions = _ShellPermissions(
        use_whitelist=int(_perm_raw.get("use_whitelist", 10)),
        neutral=int(_perm_raw.get("neutral", 45)),
        bypass_blacklist=int(_perm_raw.get("bypass_blacklist", 90)),
        access_backend=int(_perm_raw.get("access_backend", 80)),
    )

    if sandbox_url:
        logger.info("shell: dispatching via sandbox at %s", sandbox_url)
    else:
        logger.info("shell: dispatching locally (no sandbox configured)")

    blacklist = _load_blacklist()
    whitelist = _load_whitelist()

    # Caller's permission level for this cycle, resolved once. Mirrors
    # modules/sysops/__main__.py's caller_level snapshot: agent.caller is
    # set before register_agent runs and never changes mid-cycle, so this
    # closure-captured int always reflects the actual caller.
    #
    # This replaces the old (broken) backend_access check, which read
    # agent.config.permissions.level — an attribute PermissionsConfig never
    # defines (it only has minimal_tokens: bool). That check either raised
    # AttributeError on every backend_access=True call or, if it silently
    # resolved to something falsy, granted backend access to everyone
    # regardless of who was actually calling.
    caller_level = agent.caller.permission_level

    def _dispatch(command: str, local: bool = False, call_timeout: int | None = None) -> str:
        """Shared pipeline: whitelist gate → blacklist → warning → dispatch."""
        if caller_level < permissions.neutral and not _check_whitelist(command, whitelist):
            return (
                f"Blocked: permission level {caller_level} may only run whitelisted "
                f"commands. Full shell access requires permission level {permissions.neutral}."
            )
        if caller_level < permissions.bypass_blacklist:
            hit = _check_blacklist(command, blacklist)
            if hit:
                logger.warning("shell: blocked command (pattern: %s): %.120s", hit, command)
                return f"Blocked: command matched blacklist pattern '{hit}'"
        warn = _destructive_warning(command)
        prefix = f"{warn}\n" if warn else ""
        effective_timeout = min(call_timeout, max_timeout) if call_timeout is not None else default_timeout
        if local or not sandbox_url:
            output = _run_local(command, workspace, effective_timeout)
        else:
            output = _run_sandbox(command, sandbox_url, effective_timeout)
        return prefix + output

    def shell(command: str, timeout: int | None = None, backend_access: bool = False) -> str:
        if backend_access:
            if caller_level < permissions.access_backend:
                return (
                    f"Blocked: backend_access=True requires permission level "
                    f"{permissions.access_backend} (yours is {caller_level})."
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
        Requires permission level {permissions.access_backend}. Blacklist still applies in both modes.

        Callers below permission level {permissions.neutral} may only run commands
        matching an entry in whitelist.txt; every other command is blocked
        regardless of mode.

        Args:
            command: The shell command to run.
            timeout: Optional per-call timeout in seconds. Capped at the
                     configured maximum (default {max_timeout}s).
            backend_access: If True, run in the main container with full
                            network access and access to its own backend
                            files (requires permission level {permissions.access_backend}).
        """

    agent.tool_handler.register_tool(shell, always_on=True, min_permission=permissions.use_whitelist)

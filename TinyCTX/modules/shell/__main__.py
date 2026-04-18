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
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

_BLACKLIST_PATH = Path(__file__).parent / "blacklist.txt"

# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> re.Pattern:
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(escaped, re.IGNORECASE)


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


_EXIT_SEMANTICS: dict[str, callable] = {
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
            return f"[{msg}]" if msg else ""
    return f"[exit {code}]"


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
            return body.get("output", "[error: sandbox returned no output field]")
    except urllib.error.URLError as exc:
        return f"[error: cannot reach sandbox at {sandbox_url} — {exc.reason}]"
    except Exception as exc:
        return f"[error: sandbox request failed — {exc}]"


# ---------------------------------------------------------------------------
# Dispatch: local
# ---------------------------------------------------------------------------

def _run_local(command: str, cwd: Path, timeout: int) -> str:
    effective = _normalize_windows(command)
    if _IS_WINDOWS:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", effective]
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        args = ["bash", "-c", command]
        extra = {}
    try:
        result = subprocess.run(
            args, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            env=_LOCAL_ENV, **extra,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr.rstrip()}")
        annotation = _annotate_exit(command, result.returncode)
        if annotation:
            parts.append(annotation)
        return "\n".join(parts) if parts else "[no output]"
    except subprocess.TimeoutExpired:
        return f"[error: timed out after {timeout}s]"
    except FileNotFoundError as exc:
        return f"[error: shell not found — {exc}]"
    except Exception as exc:
        return f"[error: {exc}]"


# ---------------------------------------------------------------------------
# Module registration
# ---------------------------------------------------------------------------

def register(agent) -> None:
    workspace = Path(agent.config.workspace.path).expanduser().resolve()

    _extra      = agent.config.extra.get("shell", {}) if hasattr(agent.config, "extra") else {}
    timeout     = int(_extra.get("timeout", 60))
    sandbox_url = _extra.get("sandbox_url") or None

    if sandbox_url:
        logger.info("shell: dispatching via sandbox at %s", sandbox_url)
    else:
        logger.info("shell: dispatching locally")

    blacklist = _load_blacklist()

    def shell(command: str) -> str:
        """Run a shell command in the workspace directory.

        On Linux/macOS runs via bash. On Windows runs via PowerShell.
        Blocked commands return an error string without executing.
        When a sandbox is configured, the command runs in an isolated
        container with no access to the local network or Tailscale.

        Args:
            command: The shell command to run.
        """
        hit = _check_blacklist(command, blacklist)
        if hit:
            logger.warning("shell: blocked command (pattern: %s): %.120s", hit, command)
            return f"[blocked: command matched blacklist pattern '{hit}']"

        warn = _destructive_warning(command)
        prefix = f"[{warn}]\n" if warn else ""

        if sandbox_url:
            output = _run_sandbox(command, sandbox_url, timeout)
        else:
            output = _run_local(command, workspace, timeout)

        return prefix + output

    agent.tool_handler.register_tool(shell, always_on=True, min_permission=75)

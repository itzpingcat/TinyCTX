"""
modules/filesystem/shell.py

Shell execution backend for the filesystem module.
Handles platform detection, blacklist enforcement, command semantics,
destructive-command warnings, and subprocess dispatch.

On Windows: runs via PowerShell -NonInteractive
On Linux/macOS: runs via bash -c

Blacklist is always enforced regardless of platform.
Patterns are glob-style, case-insensitive, loaded from blacklist.txt.
"""
from __future__ import annotations
import re
import fnmatch
import logging
import platform
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

BLACKLIST_PATH = Path(__file__).parent / "blacklist.txt"


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a blacklist glob to a regex, treating backslash as literal."""
    escaped = re.escape(pattern)
    escaped = escaped.replace(r'\*', '.*').replace(r'\?', '.')
    return re.compile(escaped, re.IGNORECASE)

def load_blacklist(path: Path = BLACKLIST_PATH) -> list[re.Pattern]:
    if not path.exists():
        logger.warning("filesystem: blacklist not found at %s — shell is unrestricted", path)
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(glob_to_regex(line))
    logger.debug("filesystem: loaded %d blacklist patterns from %s", len(patterns), path)
    return patterns

def check_blacklist(command: str, patterns: list[re.Pattern]) -> str | None:
    for pattern in patterns:
        if pattern.fullmatch(command.lower()):
            return pattern.pattern
    return None


# ---------------------------------------------------------------------------
# Command semantics — interpret exit codes per-command
# ---------------------------------------------------------------------------
# Many commands use non-zero exit codes for informational purposes:
#   grep exit 1 = no matches (not an error)
#   diff exit 1 = files differ (expected)
#   test/[ exit 1 = condition false (not failure)
#
# Without this, the model sees "[exit 1]" and panics.

def _extract_last_command(command: str) -> str:
    """Extract the base command name from a (possibly piped) command line.
    In a pipeline, the last command determines the exit code.
    """
    # Split on pipes and take the last segment
    segments = re.split(r'\|', command)
    last = segments[-1].strip() if segments else command.strip()
    # Take the first word, stripping any leading env vars (FOO=bar cmd)
    for token in last.split():
        if '=' in token and not token.startswith('-'):
            continue  # skip VAR=value prefixes
        return token.split('/')[-1]  # basename: /usr/bin/grep → grep
    return ""


# Map of command name → function(exit_code) → (is_error: bool, message: str | None)
_COMMAND_SEMANTICS: dict[str, callable] = {
    # grep/rg: 0=matches, 1=no matches, 2+=error
    "grep":   lambda c: (c >= 2, "no matches found" if c == 1 else None),
    "rg":     lambda c: (c >= 2, "no matches found" if c == 1 else None),
    "egrep":  lambda c: (c >= 2, "no matches found" if c == 1 else None),
    "fgrep":  lambda c: (c >= 2, "no matches found" if c == 1 else None),

    # diff: 0=identical, 1=different, 2+=error
    "diff":   lambda c: (c >= 2, "files differ" if c == 1 else None),

    # test/[: 0=true, 1=false, 2+=error
    "test":   lambda c: (c >= 2, "condition is false" if c == 1 else None),
    "[":      lambda c: (c >= 2, "condition is false" if c == 1 else None),

    # find: 0=ok, 1=some dirs inaccessible (partial), 2+=error
    "find":   lambda c: (c >= 2, "some directories were inaccessible" if c == 1 else None),
}


def _interpret_exit_code(command: str, exit_code: int) -> tuple[bool, str]:
    """Interpret an exit code. Returns (is_error, annotation_string).
    is_error=False means the exit code is informational, not a failure.
    """
    if exit_code == 0:
        return (False, "")

    base_cmd = _extract_last_command(command)
    semantic = _COMMAND_SEMANTICS.get(base_cmd)

    if semantic:
        is_error, message = semantic(exit_code)
        if not is_error and message:
            return (False, f"[{message}]")
        elif not is_error:
            return (False, "")
        # Fall through to default for actual errors

    return (True, f"[exit {exit_code}]")


# ---------------------------------------------------------------------------
# Destructive command warnings — soft alerts, not blocks
# ---------------------------------------------------------------------------
# These commands are legitimate but dangerous. The warning is prepended to
# the output so the model sees it and can reconsider or confirm.

_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Git — data loss / hard to reverse
    (re.compile(r'\bgit\s+reset\s+--hard\b'),
     "warning: may discard uncommitted changes"),
    (re.compile(r'\bgit\s+push\b[^;&|\n]*\s+(--force|--force-with-lease|-f)\b'),
     "warning: may overwrite remote history"),
    (re.compile(r'\bgit\s+clean\b(?![^;&|\n]*(?:-[a-zA-Z]*n|--dry-run))[^;&|\n]*-[a-zA-Z]*f'),
     "warning: may permanently delete untracked files"),
    (re.compile(r'\bgit\s+checkout\s+(--\s+)?\.[ \t]*($|[;&|\n])'),
     "warning: may discard all working tree changes"),
    (re.compile(r'\bgit\s+restore\s+(--\s+)?\.[ \t]*($|[;&|\n])'),
     "warning: may discard all working tree changes"),
    (re.compile(r'\bgit\s+stash\s+(drop|clear)\b'),
     "warning: may permanently remove stashed changes"),
    (re.compile(r'\bgit\s+branch\s+(-D\s|--delete\s+--force|--force\s+--delete)\b'),
     "warning: may force-delete a branch"),

    # Git — safety bypass
    (re.compile(r'\bgit\s+(commit|push|merge)\b[^;&|\n]*--no-verify\b'),
     "warning: skipping safety hooks"),
    (re.compile(r'\bgit\s+commit\b[^;&|\n]*--amend\b'),
     "warning: rewriting the last commit"),

    # File deletion
    (re.compile(r'(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*[rR][a-zA-Z]*f|(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*f[a-zA-Z]*[rR]'),
     "warning: recursively force-removing files"),
    (re.compile(r'(^|[;&|\n]\s*)rm\s+-[a-zA-Z]*[rR]'),
     "warning: recursively removing files"),

    # Database
    (re.compile(r'\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b', re.IGNORECASE),
     "warning: dropping/truncating database objects"),
    (re.compile(r'\bDELETE\s+FROM\s+\w+\s*(;|"|\'|\n|$)', re.IGNORECASE),
     "warning: deleting all rows from a table"),

    # Infrastructure
    (re.compile(r'\bkubectl\s+delete\b'),
     "warning: deleting Kubernetes resources"),
    (re.compile(r'\bterraform\s+destroy\b'),
     "warning: destroying Terraform infrastructure"),
]


def get_destructive_warning(command: str) -> str | None:
    """Check if a command matches known destructive patterns.
    Returns a warning string or None.
    """
    for pattern, warning in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return warning
    return None


# ---------------------------------------------------------------------------
# Windows command normalization — harmless Unix-style read commands
# ---------------------------------------------------------------------------

def _quote_powershell_literal(token: str) -> str:
    return "'" + token.replace("'", "''") + "'"


def _normalize_windows_command(command: str) -> str:
    """Translate a few common read-only Unix commands into PowerShell.

    This keeps common LLM habits like ``ls -la`` from failing on Windows while
    preserving the original command for blacklist checks and error reporting.
    """
    if not _IS_WINDOWS:
        return command

    stripped = command.strip()
    if not stripped or any(sep in stripped for sep in ("|", ";", "&", "\n", "\r")):
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
        if token.startswith("-") and len(token) > 1 and not paths:
            chars = set(token[1:].lower())
            if not chars.issubset({"a", "l"}):
                return command
            flags.update(chars)
            continue
        paths.append(token)

    cmd_parts = ["Get-ChildItem"]
    if "a" in flags:
        cmd_parts.append("-Force")
    if paths:
        literal_paths = ", ".join(_quote_powershell_literal(path) for path in paths)
        cmd_parts.append(f"-LiteralPath {literal_paths}")
    return " ".join(cmd_parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_command(command: str, cwd: Path, timeout: int, blacklist: list[str]) -> str:
    """
    Check command against blacklist then execute it.
    Returns output as a string (stdout + stderr + exit code annotation).

    Exit codes are interpreted per-command (grep exit 1 = no matches, not
    error). Destructive commands get a soft warning prepended to output.

    Raises nothing — all errors are returned as [error: ...] strings
    so the LLM can reason about failures rather than crashing the tool call.
    """
    blocked = check_blacklist(command, blacklist)
    if blocked:
        logger.warning("filesystem: blocked command matching pattern '%s': %s", blocked, command[:120])
        return f"[blocked: command matched blacklist pattern '{blocked}']"

    # Check for destructive command — warn but don't block
    destructive_warning = get_destructive_warning(command)
    effective_command = _normalize_windows_command(command)

    if _IS_WINDOWS:
        exec_args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", effective_command]
    else:
        exec_args = ["bash", "-c", command]

    try:
        result = subprocess.run(
            exec_args, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        parts = []

        # Prepend destructive warning if applicable
        if destructive_warning:
            parts.append(f"[{destructive_warning}]")

        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr.rstrip()}")

        # Interpret exit code with command-specific semantics
        if result.returncode != 0:
            is_error, annotation = _interpret_exit_code(command, result.returncode)
            if annotation:
                parts.append(annotation)

        return "\n".join(parts) if parts else "[no output]"
    except subprocess.TimeoutExpired:
        return f"[error: timed out after {timeout}s]"
    except FileNotFoundError as e:
        return f"[error: shell not found — {e}]"
    except Exception as e:
        return f"[error: {e}]"

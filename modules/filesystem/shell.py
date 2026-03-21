"""
modules/filesystem/shell.py

Shell execution backend for the filesystem module.
Handles platform detection, blacklist enforcement, and subprocess dispatch.

On Windows: runs via PowerShell -NonInteractive
On Linux/macOS: runs via bash -c

Blacklist is always enforced regardless of platform.
Patterns are glob-style, case-insensitive, loaded from blacklist.txt.
"""
from __future__ import annotations

import fnmatch
import logging
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

BLACKLIST_PATH = Path(__file__).parent / "blacklist.txt"


def load_blacklist(path: Path = BLACKLIST_PATH) -> list[str]:
    """
    Load glob patterns from a blacklist file.
    Lines starting with # and blank lines are ignored.
    All patterns are lowercased for case-insensitive matching.
    """
    if not path.exists():
        logger.warning("filesystem: blacklist not found at %s — shell is unrestricted", path)
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line.lower())
    logger.debug("filesystem: loaded %d blacklist patterns from %s", len(patterns), path)
    return patterns


def check_blacklist(command: str, patterns: list[str]) -> str | None:
    """
    Check a command string against blacklist patterns.
    Returns the first matching pattern if blocked, None if allowed.
    Matching is case-insensitive and glob-style (fnmatch).
    """
    cmd_lower = command.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(cmd_lower, pattern):
            return pattern
    return None


def run_command(command: str, cwd: Path, timeout: int, blacklist: list[str]) -> str:
    """
    Check command against blacklist then execute it.
    Returns output as a string (stdout + stderr + exit code annotation).

    Raises nothing — all errors are returned as [error: ...] strings
    so the LLM can reason about failures rather than crashing the tool call.
    """
    blocked = check_blacklist(command, blacklist)
    if blocked:
        logger.warning("filesystem: blocked command matching pattern '%s': %s", blocked, command[:120])
        return f"[blocked: command matched blacklist pattern '{blocked}']"

    if _IS_WINDOWS:
        exec_args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        exec_args = ["bash", "-c", command]

    try:
        result = subprocess.run(
            exec_args, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr.rstrip()}")
        if result.returncode != 0:
            parts.append(f"[exit {result.returncode}]")
        return "\n".join(parts) if parts else "[no output]"
    except subprocess.TimeoutExpired:
        return f"[error: timed out after {timeout}s]"
    except FileNotFoundError as e:
        return f"[error: shell not found — {e}]"
    except Exception as e:
        return f"[error: {e}]"

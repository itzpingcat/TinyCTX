"""
onboard/bridges/matrix.py — Matrix bridge setup.

Called by bridges_setup.py when the user selects the Matrix bridge.
run() returns a bridges["matrix"] config dict, or None to skip.
"""

from __future__ import annotations

import getpass
import os
from typing import Any

import questionary
from rich.panel import Panel

from onboard.helpers import GoBack, Mode, QSTYLE, c, set_env, success, warn

DEFAULT_OPTIONS = {
    "password_env":     "MATRIX_PASSWORD",
    "device_name":      "TinyCTX",
    "store_path":       "matrix_store",
    "allowed_users":    [],
    "dm_enabled":       True,
    "room_ids":         [],
    "prefix_required":  True,
    "command_prefix":   "!",
    "max_reply_length": 16000,
    "sync_timeout_ms":  30000,
}


def run(mode: Mode) -> dict[str, Any] | None:
    """
    Guide the user through Matrix bot setup.

    Returns a bridge config dict, or None if the user skips.
    Raises GoBack to return to the bridge selection screen.
    """
    c.print()
    c.print(Panel(
        "  1. Create a Matrix account at [bold]matrix.org[/] (or your own homeserver)\n"
        "  2. Your MXID looks like: [bold]@yourname:matrix.org[/]\n"
        "  3. The bot will use your password to log in — consider a dedicated account",
        title="[bold cyan]Matrix Bot Setup[/]",
        border_style="cyan",
    ))

    raw = input("\n  Set up Matrix bridge? (y/n/back, default y): ").strip().lower()
    if raw in ("back", "b"):
        raise GoBack
    if raw in ("n", "no"):
        warn("Skipping Matrix bridge.")
        return None

    homeserver = questionary.text(
        "Homeserver URL", default="https://matrix.org", style=QSTYLE,
    ).ask() or "https://matrix.org"

    username = questionary.text(
        "Bot MXID  (e.g. @mybot:matrix.org)", default="@yourbot:matrix.org", style=QSTYLE,
    ).ask() or "@yourbot:matrix.org"

    cfg: dict[str, Any] = {
        "enabled": True,
        "options": {
            "homeserver": homeserver,
            "username":   username,
            **DEFAULT_OPTIONS,
        },
    }

    password_env = cfg["options"]["password_env"]

    if not os.environ.get(password_env):
        c.print()
        entered = _prompt_secret(f"Paste your Matrix bot password (or leave blank to set it later)")
        if entered:
            os.environ[password_env] = entered
            try:
                set_env(password_env, entered)
                success(f"{password_env} saved to your shell profile.")
            except Exception as e:
                warn(f"Could not persist {password_env} permanently ({e}) — set it manually before restarting.")
        else:
            warn(f"{password_env} not set — add it to your environment before starting.")
    else:
        success(f"{password_env} is already set.")

    return cfg


# ── helpers ───────────────────────────────────────────────────────────────────

def _prompt_secret(prompt: str) -> str:
    try:
        value = getpass.getpass(prompt + ": ")
        return value.strip() if value else ""
    except (KeyboardInterrupt, EOFError):
        return ""

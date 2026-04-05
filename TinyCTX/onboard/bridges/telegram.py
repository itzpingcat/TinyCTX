"""
onboard/bridges/telegram.py — Telegram bridge setup.

Called by bridges_setup.py when the user selects the Telegram bridge.
run() returns a bridges["telegram"] config dict, or None to skip.
"""

from __future__ import annotations

import getpass
import os
from typing import Any

from rich.panel import Panel

from onboard.helpers import GoBack, Mode, c, set_env, success, warn

DEFAULT_OPTIONS = {
    "token_env":        "TELEGRAM_BOT_TOKEN",
    "allowed_user_ids": [],
    "dm_enabled":       True,
    "max_reply_length": 4096,
    "typing_indicator": True,
}


def run(mode: Mode) -> dict[str, Any] | None:
    """
    Guide the user through Telegram bot setup.

    Returns a bridge config dict, or None if the user skips.
    Raises GoBack to return to the bridge selection screen.
    """
    c.print()
    c.print(Panel(
        "  1. Open Telegram and search for [bold]@BotFather[/]\n"
        "  2. Send [bold]/newbot[/] and follow the prompts\n"
        "  3. Copy the token BotFather gives you (looks like 123456:ABC-DEF…)",
        title="[bold cyan]Telegram Bot Setup[/]",
        border_style="cyan",
    ))

    raw = input("\n  Set up Telegram bridge? (y/n/back, default y): ").strip().lower()
    if raw in ("back", "b"):
        raise GoBack
    if raw in ("n", "no"):
        warn("Skipping Telegram bridge.")
        return None

    cfg: dict[str, Any] = {"enabled": True, "options": dict(DEFAULT_OPTIONS)}
    token_env = cfg["options"]["token_env"]

    if not os.environ.get(token_env):
        c.print()
        entered = _prompt_secret("Paste your Telegram bot token (or leave blank to set it later)")
        if entered:
            os.environ[token_env] = entered
            try:
                set_env(token_env, entered)
                success(f"{token_env} saved to your shell profile.")
            except Exception as e:
                warn(f"Could not persist {token_env} permanently ({e}) — set it manually before restarting.")
        else:
            warn(f"{token_env} not set — add it to your environment before starting.")
    else:
        success(f"{token_env} is already set.")

    return cfg


# ── helpers ───────────────────────────────────────────────────────────────────

def _prompt_secret(prompt: str) -> str:
    try:
        value = getpass.getpass(prompt + ": ")
        return value.strip() if value else ""
    except (KeyboardInterrupt, EOFError):
        return ""

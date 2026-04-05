"""
onboard/bridges/discord.py — Discord bridge setup.

Called by bridges_setup.py when the user selects the Discord bridge.
run() returns a bridges["discord"] config dict, or None to skip.
"""

from __future__ import annotations

import getpass
import os
from typing import Any

from rich.panel import Panel

from onboard.helpers import GoBack, Mode, c, set_env, success, warn

DEFAULT_OPTIONS = {
    "token_env":       "DISCORD_BOT_TOKEN",
    "allowed_users":   [],
    "dm_enabled":      True,
    "guild_ids":       [],
    "prefix_required": True,
    "command_prefix":  "!",
    "max_reply_length": 1900,
    "typing_indicator": True,
}


def run(mode: Mode) -> dict[str, Any] | None:
    """
    Guide the user through Discord bot setup.

    Returns a bridge config dict, or None if the user skips.
    Raises GoBack to return to the bridge selection screen.
    """
    c.print()
    c.print(Panel(
        "  1. Go to [bold]discord.com/developers/applications[/]\n"
        "  2. Click [bold]New Application[/] and give it a name\n"
        "  3. Go to [bold]Bot[/] → click [bold]Add Bot[/]\n"
        "  4. Under [bold]Privileged Gateway Intents[/], enable:\n"
        "       • Message Content Intent\n"
        "       • Server Members Intent\n"
        "  5. Click [bold]Reset Token[/] and copy it",
        title="[bold cyan]Discord Bot Setup[/]",
        border_style="cyan",
    ))

    raw = input("\n  Set up Discord bridge? (y/n/back, default y): ").strip().lower()
    if raw in ("back", "b"):
        raise GoBack
    if raw in ("n", "no"):
        warn("Skipping Discord bridge.")
        return None

    cfg: dict[str, Any] = {"enabled": True, "options": dict(DEFAULT_OPTIONS)}

    token_env = cfg["options"]["token_env"]

    if not os.environ.get(token_env):
        c.print()
        entered = _prompt_secret(f"Paste your Discord bot token (or leave blank to set it later)")
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

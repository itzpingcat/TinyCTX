"""
bridges/discord/__main__.py — Entry point for the Discord bridge.

All implementation lives in the sub-modules:
  bridge.py    DiscordBridge — routing, access control, attachment handling
  turn.py      handle_turn, typing_keepalive — agent turn execution
  commands.py  sync_app_commands, slash-command interaction handlers
  cursors.py   CursorStore, make_session_node — session persistence
  compat.py    CompatRules — proxy-bot delay rules
  mentions.py  humanize_mentions, dehumanize_mentions

Config (in config.yaml under bridges.discord.options) — see bridge.py DEFAULTS
for the full list of options and their descriptions.

Token setup:
  export DISCORD_BOT_TOKEN=your-bot-token-here

Required bot intents (Discord Developer Portal):
  - Message Content Intent (privileged — must be enabled manually)
  - Server Members Intent (optional but helpful for username resolution)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .bridge import DiscordBridge

if TYPE_CHECKING:
    from TinyCTX.runtime import Runtime


async def run(runtime: "Runtime") -> None:
    """Entry point called by main.py bridge loader."""
    bridge_cfg = runtime.config.bridges.get("discord")
    options: dict = bridge_cfg.options if bridge_cfg else {}
    bridge = DiscordBridge(runtime, options)
    await bridge.run()

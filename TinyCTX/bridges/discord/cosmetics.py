"""
bridges/discord/cosmetics.py — Apply configured bot presence/identity on connect.

Config (bridges.discord.options.cosmetics in config.yaml):

    bridges:
      discord:
        options:
          cosmetics:
            status: online          # online | idle | dnd | invisible
            activity_type: custom   # playing | watching | listening | competing | streaming | custom
            activity_text: "context window go brrr"
            activity_emoji: "🧠"     # custom only — optional, shown before the text
            nickname: ""            # optional, applied per-guild in allowed_servers

activity_type "custom" is the speech-bubble status Discord shows attached to
a user/bot's avatar (an emoji + free text, no "Playing"/"Watching" verb
prefix) — discord.py's CustomActivity / activity type 4.

status/activity are applied via client.change_presence() — cheap, no rate
limit, safe to re-apply on every reconnect. nickname is applied per-guild via
guild.me.edit() — one call per allowed guild, only when non-empty.

Deliberately NOT config-driven here: username, avatar, banner. Those go
through client.user.edit() and Discord rate-limits profile edits to roughly
2/hour — reapplying them on every on_ready would risk a 429 loop. Leave them
as one-off manual changes (Discord dev portal, or a future /cosmetics
command) rather than steady-state config.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from TinyCTX.bridges.discord.bridge import DiscordBridge

logger = logging.getLogger(__name__)

DEFAULTS = {
    "status": "",          # "" = leave default (online, no explicit call skipped below)
    "activity_type": "",   # "" = no activity set
    "activity_text": "",
    "activity_emoji": "",  # custom only
    "nickname": "",
}

_STATUS_MAP = {
    "online":    discord.Status.online,
    "idle":      discord.Status.idle,
    "dnd":       discord.Status.dnd,
    "invisible": discord.Status.invisible,
}

_ACTIVITY_TYPE_MAP = {
    "playing":   discord.ActivityType.playing,
    "watching":  discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}


async def apply(bridge: "DiscordBridge", options: dict) -> None:
    """Apply configured presence + per-guild nickname. Called from _on_ready."""
    cfg = {**DEFAULTS, **(options.get("cosmetics") or {})}

    await _apply_presence(bridge, cfg)
    await _apply_nickname(bridge, cfg)


async def _apply_presence(bridge: "DiscordBridge", cfg: dict) -> None:
    status_name = str(cfg["status"]).strip().lower()
    activity_type_name = str(cfg["activity_type"]).strip().lower()
    activity_text = str(cfg["activity_text"]).strip()
    activity_emoji = str(cfg["activity_emoji"]).strip()

    if not status_name and not activity_type_name:
        return  # nothing configured — leave discord.py's default presence alone

    status = None
    if status_name:
        status = _STATUS_MAP.get(status_name)
        if status is None:
            logger.warning(
                "[cosmetics] unknown status %r — valid: %s",
                status_name, ", ".join(_STATUS_MAP),
            )

    activity = None
    if activity_type_name == "custom":
        if activity_text or activity_emoji:
            activity = discord.CustomActivity(name=activity_text, emoji=activity_emoji or None)
        else:
            logger.warning("[cosmetics] activity_type 'custom' needs activity_text or activity_emoji — skipping")
    elif activity_type_name == "streaming":
        if activity_text:
            # Streaming activities need a URL — reuse activity_text as the
            # display name and point at a placeholder Twitch URL, since
            # Discord requires a valid streaming host to show "Streaming".
            activity = discord.Streaming(name=activity_text, url="https://www.twitch.tv/")
        else:
            logger.warning("[cosmetics] activity_type 'streaming' needs activity_text — skipping")
    elif activity_type_name:
        activity_type = _ACTIVITY_TYPE_MAP.get(activity_type_name)
        if activity_type is None:
            logger.warning(
                "[cosmetics] unknown activity_type %r — valid: %s, streaming, custom",
                activity_type_name, ", ".join(_ACTIVITY_TYPE_MAP),
            )
        elif activity_text:
            activity = discord.Activity(type=activity_type, name=activity_text)
        else:
            logger.warning("[cosmetics] activity_type set but activity_text is empty — skipping")

    try:
        await bridge._client.change_presence(status=status, activity=activity)
        logger.info(
            "[cosmetics] presence set: status=%s activity=%s %r",
            status_name or "(default)", activity_type_name or "(none)", activity_text,
        )
    except Exception:
        logger.exception("[cosmetics] failed to set presence")


async def _apply_nickname(bridge: "DiscordBridge", cfg: dict) -> None:
    nickname = str(cfg["nickname"]).strip()
    if not nickname:
        return

    for guild_id in bridge._allowed_servers:
        guild = bridge._client.get_guild(guild_id)
        if guild is None or guild.me is None:
            continue
        try:
            await guild.me.edit(nick=nickname)
            logger.info("[cosmetics] nickname set to %r in guild %s", nickname, guild_id)
        except Exception:
            logger.exception("[cosmetics] failed to set nickname in guild %s", guild_id)

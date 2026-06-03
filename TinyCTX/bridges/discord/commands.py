"""
bridges/discord/commands.py — Slash command registration and dispatch.

Builds Discord native app_commands from CommandRegistry entries, registers
/reset and /shutdown, and handles their interactions. Imported and called
by DiscordBridge.

Slash command registration happens in two stages:
  1. sync_app_commands() walks CommandRegistry and builds one
     discord.app_commands.Command (or Group+subcommand) per entry.
  2. app_commands.CommandTree.sync() pushes the full list to Discord's API.
     Sync is global and takes up to 1 hour to propagate.

Commands with a subcommand are registered as Discord subcommands under a Group,
appearing as "/namespace" → select "subcommand". Bare "/namespace" commands
(no sub) are registered as flat slash commands.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from TinyCTX.bridges.discord.bridge import DiscordBridge

logger = logging.getLogger(__name__)


async def sync_app_commands(bridge: "DiscordBridge") -> None:
    """
    Walk CommandRegistry and register commands as native Discord slash commands.
    Always registers /reset and /shutdown from bridge config.
    """
    tree   = bridge._tree
    tree.clear_commands(guild=None)

    # /reset
    reset_cmd_name = bridge._reset_command.lstrip("/").replace(" ", "_")

    @tree.command(name=reset_cmd_name, description="Reset the current session")
    async def _reset_slash(interaction: discord.Interaction) -> None:  # noqa: F841
        await handle_reset_interaction(bridge, interaction)

    # /shutdown
    shutdown_cmd_name = bridge._shutdown_command.lstrip("/").replace(" ", "_")

    @tree.command(name=shutdown_cmd_name, description="Kill the TinyCTX gateway (admin only)")
    async def _shutdown_slash(interaction: discord.Interaction) -> None:  # noqa: F841
        await handle_shutdown_interaction(bridge, interaction)

    # Group CommandRegistry entries by namespace.
    grouped: dict[str, dict[str, tuple[str, str, str]]] = {}
    for cmd_str, help_text in bridge._runtime.commands.list_commands():
        parts     = cmd_str.lstrip("/").split()
        namespace = parts[0]
        sub       = parts[1] if len(parts) > 1 else ""
        grouped.setdefault(namespace, {})[sub] = (
            help_text or f"Run {cmd_str}", namespace, sub
        )

    for namespace, subs in grouped.items():  # type: ignore[assignment]
        has_bare   = "" in subs
        named_subs = {k: v for k, v in subs.items() if k}

        if not named_subs:
            desc, ns, sub = subs[""]

            def _make_flat(ns: str, sub: str) -> None:
                @tree.command(name=ns, description=desc)
                async def _handler(interaction: discord.Interaction) -> None:  # noqa: F841
                    await handle_command_interaction(bridge, interaction, ns, sub)

            _make_flat(ns, sub)
            continue

        group = app_commands.Group(name=namespace, description=f"{namespace} commands")

        for sub_name, (desc, ns, sub) in named_subs.items():
            def _make_sub(ns: str, sub: str, desc: str) -> None:
                @group.command(name=sub, description=desc)
                async def _sub_handler(interaction: discord.Interaction) -> None:  # noqa: F841
                    await handle_command_interaction(bridge, interaction, ns, sub)

            _make_sub(ns, sub_name, desc)

        if has_bare:
            desc, ns, _ = subs[""]

            def _make_bare_as_run(ns: str, desc: str) -> None:
                @group.command(name="run", description=desc)
                async def _run_handler(interaction: discord.Interaction) -> None:  # noqa: F841
                    await handle_command_interaction(bridge, interaction, ns, "")

            _make_bare_as_run(ns, desc)

        tree.add_command(group)

    try:
        synced = await tree.sync()
        logger.info(
            "Discord bridge: synced %d app command(s) to Discord", len(synced)
        )
    except Exception:
        logger.exception("Discord bridge: failed to sync app commands")


async def handle_reset_interaction(
    bridge: "DiscordBridge", interaction: discord.Interaction
) -> None:
    """Handle the /reset slash command."""
    from TinyCTX.bridges.discord.cursors import make_session_node

    channel = interaction.channel
    is_dm   = isinstance(channel, discord.DMChannel)
    await interaction.response.defer(ephemeral=False)
    user_id = interaction.user.id

    if not is_dm and not bridge._is_admin(user_id):
        await interaction.followup.send(
            "⛔ Only admins can reset the session.", ephemeral=True
        )
        return

    if is_dm:
        cursor_key = f"dm:{interaction.user.id}"
    elif isinstance(channel, discord.Thread):
        cursor_key = f"thread:{channel.id}"
    else:
        cursor_key = f"group:{channel.id}" if channel else None

    if cursor_key:
        bridge._reset_epoch[cursor_key] = bridge._reset_epoch.get(cursor_key, 0) + 1
        new_node_id = make_session_node(bridge._runtime.db, cursor_key)
        bridge._store.set(cursor_key, new_node_id)
        logger.info(
            "Discord: session reset by %s — new branch %s for %s",
            interaction.user.id, new_node_id, cursor_key,
        )
    await interaction.followup.send("✅ Session reset.", ephemeral=True)


async def handle_shutdown_interaction(
    bridge: "DiscordBridge", interaction: discord.Interaction
) -> None:
    """Handle the /shutdown slash command — kills the gateway process."""
    await interaction.response.defer(ephemeral=True)

    if not bridge._is_admin(interaction.user.id):
        await interaction.followup.send(
            "⛔ Only admins can shut down the gateway.", ephemeral=True
        )
        return

    cfg         = bridge._runtime.config
    gateway_url = f"http://{cfg.gateway.host}:{cfg.gateway.port}"
    api_key     = cfg.gateway.api_key or ""

    logger.warning(
        "Discord: /shutdown invoked by %s (%s) — sending POST /v1/shutdown",
        interaction.user.name, interaction.user.id,
    )

    try:
        req = urllib.request.Request(
            f"{gateway_url}/v1/shutdown",
            method="POST",
            data=b"{}",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        await interaction.followup.send("🔴 Gateway is shutting down.", ephemeral=True)
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            await interaction.followup.send("🔴 Gateway is shutting down.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⚠️ Shutdown request failed (HTTP {exc.code}).", ephemeral=True
            )
    except Exception as exc:
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            await interaction.followup.send("🔴 Gateway is shutting down.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⚠️ Shutdown failed: {exc}", ephemeral=True
            )


async def handle_command_interaction(
    bridge: "DiscordBridge",
    interaction: discord.Interaction,
    namespace: str,
    sub: str,
) -> None:
    """Dispatch a registered CommandRegistry command via a native interaction."""
    await interaction.response.defer(ephemeral=False)

    channel = interaction.channel
    if isinstance(channel, discord.DMChannel):
        cursor_key = f"dm:{interaction.user.id}"
    elif isinstance(channel, discord.Thread):
        cursor_key = f"thread:{channel.id}"
    else:
        cursor_key = f"group:{channel.id}" if channel else None

    node_id = bridge._store.get(cursor_key) or "" if cursor_key else ""

    reply_parts: list[str] = []

    async def _send_reply(text: str) -> None:
        reply_parts.append(text)

    ctx = {
        "channel":     channel,
        "interaction": interaction,
        "followup":    interaction.followup,
        "guild":       interaction.guild,
        "bridge":      bridge,
        "runtime":     bridge._runtime,
        "cursor":      node_id,
        "send":        _send_reply,
    }

    text    = f"/{namespace} {sub}".strip() if sub else f"/{namespace}"
    handled = await bridge._runtime.commands.dispatch(text, ctx)

    if not handled:
        await interaction.followup.send("⚠️ Command not found.", ephemeral=True)
        return

    if reply_parts:
        combined = "\n".join(reply_parts)
        for i in range(0, len(combined), bridge._max_len):
            await interaction.followup.send(combined[i : i + bridge._max_len])
    else:
        await interaction.followup.send("✅ Done.", ephemeral=True)

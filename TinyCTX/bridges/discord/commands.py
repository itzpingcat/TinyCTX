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
    grouped: dict[str, list] = {}
    for entry in bridge._runtime.commands.entries():
        grouped.setdefault(entry.namespace, []).append(entry)

    for namespace, entries in grouped.items():
        bare   = next((e for e in entries if e.sub == ""), None)
        named  = [e for e in entries if e.sub]

        if not named:
            # Flat command — no subcommands.
            e = bare
            _register_command(tree, bridge, e, parent=None)
            continue

        group = app_commands.Group(name=namespace, description=f"{namespace} commands")
        for e in named:
            _register_command(tree, bridge, e, parent=group)
        if bare:
            _register_command(tree, bridge, bare, parent=group, name_override="run")
        tree.add_command(group)

    try:
        synced = await tree.sync()
        logger.info("Discord bridge: synced %d app command(s) to Discord", len(synced))
    except Exception:
        logger.exception("Discord bridge: failed to sync app commands")


def _register_command(
    tree_or_group,
    bridge: "DiscordBridge",
    entry,
    *,
    parent=None,
    name_override: str | None = None,
) -> None:
    """
    Register a single CommandRegistry entry as a Discord app command.
    Builds a typed handler from entry.params so no command-specific
    knowledge is needed here.
    """
    name  = name_override or (entry.sub or entry.namespace)
    desc  = entry.help or f"Run /{entry.namespace} {entry.sub}".strip()
    dest  = parent if parent is not None else tree_or_group
    ns, sub = entry.namespace, entry.sub
    params = entry.params  # list of (name, type, description)

    if not params:
        @dest.command(name=name, description=desc)
        async def _handler(interaction: discord.Interaction) -> None:  # noqa: F841
            await handle_command_interaction(bridge, interaction, ns, sub)
        return

    # Build a dynamic function with the correct typed signature so discord.py
    # introspects it and presents proper input fields to the user.
    # We use exec() because discord.py reads __annotations__ at decoration time
    # and there's no public API to inject parameters dynamically.
    param_parts = ", ".join(
        f"{p_name}: {p_type.__name__}" for p_name, p_type, _ in params
    )
    arg_list = ", ".join(p_name for p_name, _, _ in params)

    fn_src = (
        f"async def _typed_handler(interaction, {param_parts}):\n"
        f"    await _dispatch(bridge, interaction, ns, sub, [{arg_list}])\n"
    )
    globs = {"_dispatch": _dispatch_with_args, "bridge": bridge, "ns": ns, "sub": sub}
    exec(fn_src, globs)  # noqa: S102
    fn = globs["_typed_handler"]

    # Apply @app_commands.describe directly using the kwargs dict.
    fn = app_commands.describe(**{p_name: p_desc for p_name, _, p_desc in params})(fn)

    dest.command(name=name, description=desc)(fn)


async def _dispatch_with_args(
    bridge: "DiscordBridge",
    interaction: discord.Interaction,
    namespace: str,
    sub: str,
    arg_values: list,
) -> None:
    """Collect typed Discord param values and dispatch through CommandRegistry."""
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

    async def _send(text: str) -> None:
        reply_parts.append(text)

    ctx = {
        "channel":     channel,
        "interaction": interaction,
        "followup":    interaction.followup,
        "guild":       interaction.guild,
        "bridge":      bridge,
        "runtime":     bridge._runtime,
        "cursor":      node_id,
        "send":        _send,
    }

    # Convert typed values back to strings for the text-dispatch path.
    args = [str(v) for v in arg_values]
    text = f"/{namespace} {sub} " + " ".join(args) if sub else f"/{namespace} " + " ".join(args)
    handled = await bridge._runtime.commands.dispatch(text.strip(), ctx)

    if not handled:
        await interaction.followup.send("⚠️ Command not found.", ephemeral=True)
        return

    if reply_parts:
        combined = "\n".join(reply_parts)
        for i in range(0, len(combined), bridge._max_len):
            await interaction.followup.send(combined[i : i + bridge._max_len])
    else:
        await interaction.followup.send("✅ Done.", ephemeral=True)


async def handle_reset_interaction(
    bridge: "DiscordBridge", interaction: discord.Interaction
) -> None:
    """Handle the /reset slash command."""
    from TinyCTX.bridges.discord.cursors import make_session_node

    channel = interaction.channel
    is_dm   = isinstance(channel, discord.DMChannel)
    await interaction.response.defer(ephemeral=False)
    user_id = interaction.user.id

    if not is_dm and not bridge._can_reset(user_id):
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
        bridge._pending.pop(cursor_key, None)
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

    if not bridge._can_reset(interaction.user.id):
        await interaction.followup.send(
            "? Only admins can shut down the gateway.", ephemeral=True
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

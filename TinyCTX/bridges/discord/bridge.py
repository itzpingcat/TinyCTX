"""
bridges/discord/bridge.py — DiscordBridge: event routing, access control,
                             attachment handling, and session management.

This is the central class. Responsibilities kept here:
  - discord.py client setup and event registration
  - Access-control checks (allowed_users_dm, allowed_servers, admin_users)
  - on_message routing (DM / group channel / thread)
  - Attachment + forwarded-message extraction
  - Cursor read/create/advance wrappers
  - Delegating turn execution to turn.handle_turn()
  - Delegating slash-command sync to commands.sync_app_commands()

Decomposed sub-modules:
  mentions.py  — humanize_mentions / dehumanize_mentions
  compat.py    — CompatRules (proxy-bot delay rules)
  cursors.py   — CursorStore, make_session_node
  turn.py      — handle_turn, typing_keepalive
  commands.py  — sync_app_commands, slash-command interaction handlers
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from TinyCTX.contracts import (
    Attachment,
    content_type_for,
    InboundMessage,
    Platform,
)

from .compat   import CompatRules
from .cursors  import CursorStore, make_session_node
from .mentions import humanize_mentions, dehumanize_mentions
from . import commands as _cmd_module
from . import turn     as _turn_module

if TYPE_CHECKING:
    from TinyCTX.runtime import Runtime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS = {
    "token_env": "DISCORD_BOT_TOKEN",
    "allowed_users_dm": [],
    "allowed_servers": {},
    "admin_users": [],
    "dm_enabled": True,
    "prefix_required": True,
    "command_prefix": "!",
    "reset_command": "/reset",
    "shutdown_command": "/shutdown",
    "buffer_timeout_s": 0,
    "buffer_head_lines": 2,
    "buffer_tail_lines": 10,
    "max_reply_length": 1900,
    "typing_indicator": True,
    "typing_on_thinking": True,
    "typing_on_tools": True,
    "typing_on_reply": True,
}


# ---------------------------------------------------------------------------
# DiscordBridge
# ---------------------------------------------------------------------------

class DiscordBridge:
    def __init__(self, runtime: "Runtime", options: dict) -> None:
        self._runtime = runtime
        self._opts    = {**DEFAULTS, **options}

        self._max_len:            int   = int(self._opts["max_reply_length"])
        self._typing:             bool  = bool(self._opts["typing_indicator"])
        self._typing_on_thinking: bool  = bool(self._opts["typing_on_thinking"])
        self._typing_on_tools:    bool  = bool(self._opts["typing_on_tools"])
        self._typing_on_reply:    bool  = bool(self._opts["typing_on_reply"])
        self._prefix:             str   = str(self._opts["command_prefix"])
        self._prefix_required:    bool  = bool(self._opts["prefix_required"])
        self._reset_command:      str   = str(self._opts["reset_command"])
        self._shutdown_command:   str   = str(self._opts["shutdown_command"])
        self._dm_enabled:         bool  = bool(self._opts["dm_enabled"])

        raw_servers = self._opts["allowed_servers"]
        self._allowed_servers: dict[int, set[int]] = {
            int(guild_id): {int(c) for c in channels}
            for guild_id, channels in raw_servers.items()
        }
        self._buffer_timeout_s:  float = float(self._opts["buffer_timeout_s"])
        self._buffer_head_lines: int   = int(self._opts["buffer_head_lines"])
        self._buffer_tail_lines: int   = int(self._opts["buffer_tail_lines"])

        self._allowed_users_dm: set[int] = {int(u) for u in self._opts["allowed_users_dm"]}
        self._admin_users:      set[int] = {int(u) for u in self._opts["admin_users"]}

        # In-flight state (not persisted)
        self._typing_active:    dict[str, asyncio.Event]          = {}
        self._tasks:            set[asyncio.Task]                  = set()
        self._active_channels:  dict[str, discord.abc.Messageable] = {}
        self._node_to_cursor:   dict[str, str]                    = {}
        self._reset_epoch:      dict[str, int]                    = {}
        self._lane_locks:       dict[str, asyncio.Lock]           = {}

        # Persisted cursor store
        workspace   = Path(runtime.config.workspace.path).expanduser().resolve()
        cursors_dir = workspace / "cursors"
        cursors_dir.mkdir(parents=True, exist_ok=True)
        self._store = CursorStore(cursors_dir)

        # Compat rules
        _bridge_dir  = Path(__file__).parent
        self._compat = CompatRules(_bridge_dir / "compat.json")

        # discord.py client
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members         = True
        self._client = discord_commands.Bot(command_prefix="\\", intents=intents)
        self._tree   = self._client.tree

        # Register event handlers
        _bridge = self

        async def on_ready() -> None:
            await _bridge._on_ready()

        async def on_message(message: discord.Message) -> None:
            await _bridge._on_message(message)

        self._client.event(on_ready)
        self._client.event(on_message)

        # Stale-interaction error handler
        @self._tree.error
        async def _on_tree_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            if isinstance(error, app_commands.CommandNotFound):
                logger.warning(
                    "Discord: received interaction for unknown command %r "
                    "(stale Discord registration or mid-sync).",
                    getattr(interaction.data, "name", "?"),
                )
                try:
                    msg = (
                        "⚠️ This command isn't available yet — "
                        "the bot may still be starting up. Please try again in a moment."
                    )
                    if not interaction.response.is_done():
                        await interaction.response.send_message(msg, ephemeral=True)
                    else:
                        await interaction.followup.send(msg, ephemeral=True)
                except Exception:
                    logger.debug("Discord: could not send CommandNotFound notice", exc_info=True)
                return
            logger.error(
                "Discord: unhandled app command error for %r",
                getattr(interaction.data, "name", "?"),
                exc_info=error,
            )

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    def _get_cursor(self, cursor_key: str) -> str | None:
        return self._store.get(cursor_key)

    def _get_or_create_cursor(self, cursor_key: str) -> str:
        node_id = self._store.get(cursor_key)
        if not node_id:
            node_id = make_session_node(self._runtime.db, cursor_key)
            self._store.set(cursor_key, node_id)
            logger.info("Discord: created cursor %s -> %s", cursor_key, node_id)
        return node_id

    def _get_or_create_thread_cursor(self, thread_id: str, channel_id: str) -> str:
        cursor_key = f"thread:{thread_id}"
        node_id = self._store.get(cursor_key)
        if not node_id:
            parent_node_id = self._store.get_msg_node(thread_id)
            if parent_node_id is None:
                parent_node_id = self._store.get(f"group:{channel_id}")
            if parent_node_id is None:
                node_id = make_session_node(self._runtime.db, cursor_key)
                logger.info(
                    "Discord: thread %s no parent — fresh branch %s",
                    thread_id, node_id,
                )
            else:
                node_id = parent_node_id
                logger.info(
                    "Discord: thread %s forked from node %s",
                    thread_id, parent_node_id,
                )
            self._store.set(cursor_key, node_id)
        return node_id

    def _advance_cursor(self, cursor_key: str, new_tail: str) -> None:
        self._store.set(cursor_key, new_tail)
        logger.info("Discord: cursor %s advanced to %s", cursor_key, new_tail)

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    def _resolve_permission_level(self, member_roles: list | None) -> int:
        role_map = self._opts.get("role_permissions", {})
        default  = int(self._opts.get("default_permission", 25))
        if not member_roles or not role_map:
            return default
        int_map = {int(k): int(v) for k, v in role_map.items()}
        for role in sorted(member_roles, key=lambda r: r.position, reverse=True):
            if role.id in int_map:
                return int_map[role.id]
        return default

    def _is_allowed_dm(self, user_id: int) -> bool:
        if not self._allowed_users_dm:
            return True
        return user_id in self._allowed_users_dm

    def _is_allowed_server(self, guild_id: int, channel_id: int) -> bool:
        if guild_id not in self._allowed_servers:
            return False
        allowed_channels = self._allowed_servers[guild_id]
        return not allowed_channels or channel_id in allowed_channels

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admin_users

    def _is_group_trigger(self, text: str) -> bool:
        if not self._prefix_required:
            return True
        if text.startswith(self._prefix):
            return True
        bot_name = self._client.user.name if self._client.user else ""
        if bot_name and f"@{bot_name}" in text:
            return True
        return False

    def _dehumanize_mentions(self, text: str) -> str:
        return dehumanize_mentions(text, self._runtime)

    # ------------------------------------------------------------------
    # Attachment / forwarded-message helpers
    # ------------------------------------------------------------------

    async def _extract_forwarded(
        self, message: discord.Message
    ) -> tuple[str, tuple]:
        snapshots = getattr(message, "message_snapshots", None)
        if not snapshots:
            return "", ()

        snap  = snapshots[0]
        lines: list[str] = []
        extra_attachments: list = []

        for a in getattr(snap, "attachments", []):
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(a.url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            mime = a.content_type or "application/octet-stream"
                            extra_attachments.append(
                                Attachment(
                                    filename=a.filename, data=data, mime_type=mime
                                )
                            )
                            lines.append(f"> [image: {a.filename}]")
                        else:
                            lines.append(f"> [attachment: {a.filename}]")
            except Exception:
                logger.warning(
                    "Discord: failed to download forwarded attachment %s", a.filename
                )
                lines.append(f"> [attachment: {a.filename}]")

        for _ in getattr(snap, "embeds", []):
            lines.append("> [embed]")

        for sticker in getattr(snap, "stickers", []):
            lines.append(f"> [sticker: {sticker.name}]")

        content = getattr(snap, "content", "") or ""
        for line in content.splitlines():
            lines.append(f"> {line}" if line else ">")

        if not lines:
            return "", ()

        text_block = "[Forwarded message]\n" + "\n".join(lines)
        return text_block, tuple(extra_attachments)

    async def _fetch_attachments(self, message: discord.Message) -> tuple:
        if not message.attachments:
            return ()
        fetched = []
        for a in message.attachments:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(a.url) as resp:
                        data = await resp.read()
                mime = a.content_type or "application/octet-stream"
                fetched.append(
                    Attachment(filename=a.filename, data=data, mime_type=mime)
                )
            except Exception:
                logger.warning(
                    "Discord: failed to download attachment %s", a.filename
                )
        return tuple(fetched)

    # ------------------------------------------------------------------
    # Discord event callbacks
    # ------------------------------------------------------------------

    async def _on_ready(self) -> None:
        logger.info(
            "Discord bridge connected as %s (id=%s)",
            self._client.user,
            self._client.user.id if self._client.user else "?",
        )
        if not self._allowed_users_dm:
            logger.warning(
                "Discord bridge: allowed_users_dm is empty — the bot will respond "
                "to DMs from anyone."
            )
        if not self._allowed_servers:
            logger.warning(
                "Discord bridge: allowed_servers is empty — the bot will not respond "
                "in any server."
            )
        if not self._admin_users:
            logger.warning(
                "Discord bridge: admin_users is empty — nobody can use /%s in group sessions.",
                self._reset_command.lstrip("/"),
            )
        await _cmd_module.sync_app_commands(self)

    async def _on_message(self, message: discord.Message) -> None:
        if message.author.bot and (
            self._client.user is None
            or message.author.id == self._client.user.id
        ):
            return

        # ── Thread ───────────────────────────────────────────────────
        if isinstance(message.channel, discord.Thread):
            await self._handle_thread_message(message)
            return

        # ── DM ───────────────────────────────────────────────────────
        if isinstance(message.channel, discord.DMChannel):
            if not self._dm_enabled:
                return
            if not self._is_allowed_dm(message.author.id):
                logger.debug(
                    "Discord: ignoring DM from unauthorized user %s (%s)",
                    message.author.id, message.author.name,
                )
                return
            text        = message.content.strip()
            attachments = await self._fetch_attachments(message)
            fwd_text, fwd_attachments = await self._extract_forwarded(message)
            if fwd_text:
                text = f"{text}\n{fwd_text}".strip()
                attachments = attachments + fwd_attachments
            if not text and not attachments:
                return

            cursor_key = f"dm:{message.author.id}"
            author     = self._runtime.users.resolve_user(
                platform=Platform.DISCORD,
                user_id=str(message.author.id),
                username=message.author.name,
                display_name=message.author.display_name,
            )
            bot_id = self._client.user.id if self._client.user else None
            ref    = message.reference
            resolved_dm = ref.resolved if ref else None
            reply_to_author_dm: str | None = None
            if (
                isinstance(resolved_dm, discord.Message)
                and resolved_dm.author.id != bot_id
            ):
                resolved_user = self._runtime.users.resolve_user(
                    platform=Platform.DISCORD,
                    user_id=str(resolved_dm.author.id),
                    username=resolved_dm.author.name,
                    display_name=resolved_dm.author.display_name,
                )
                reply_to_author_dm = resolved_user.username
            msg = InboundMessage(
                tail_node_id="",
                author=author,
                content_type=content_type_for(text, bool(attachments)),
                text=text,
                message_id=str(message.id),
                timestamp=time.time(),
                attachments=attachments,
                server_name=None,
                channel_name=None,
                trigger=True,
                reply_to_author=reply_to_author_dm,
            )
            task = asyncio.create_task(
                _turn_module.handle_turn(self, msg, message.channel, cursor_key)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        # ── Group channel ─────────────────────────────────────────────
        if not message.guild or not self._is_allowed_server(
            message.guild.id, message.channel.id
        ):
            logger.debug(
                "Discord: ignoring message in unallowed guild/channel %s/%s",
                getattr(message.guild, "id", None),
                message.channel.id,
            )
            return

        channel_id = str(message.channel.id)
        cursor_key = f"group:{channel_id}"
        raw_text   = message.content.strip()
        bot_id     = self._client.user.id if self._client.user else None
        text        = await humanize_mentions(raw_text, self._client)
        attachments = await self._fetch_attachments(message)
        fwd_text, fwd_attachments = await self._extract_forwarded(message)
        if fwd_text:
            text = f"{text}\n{fwd_text}".strip()
            attachments = attachments + fwd_attachments

        # Auto-prefix the bot name when replying to it (makes trigger detection work).
        bot_name = self._client.user.name if self._client.user else None
        if bot_id and bot_name:
            ref      = message.reference
            resolved = ref.resolved if ref else None
            if (
                isinstance(resolved, discord.Message)
                and resolved.author.id == bot_id
                and f"@{bot_name}" not in text
            ):
                text = f"@{bot_name} {text}"
        if not text and not attachments:
            return

        author     = self._runtime.users.resolve_user(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.name,
            display_name=message.author.display_name,
        )
        is_trigger = self._is_group_trigger(text)
        ref_group  = message.reference
        resolved_group = ref_group.resolved if ref_group else None
        reply_to_author_group: str | None = None
        if (
            isinstance(resolved_group, discord.Message)
            and resolved_group.author.id != bot_id
        ):
            resolved_user_group = self._runtime.users.resolve_user(
                platform=Platform.DISCORD,
                user_id=str(resolved_group.author.id),
                username=resolved_group.author.name,
                display_name=resolved_group.author.display_name,
            )
            reply_to_author_group = resolved_user_group.username
        msg = InboundMessage(
            tail_node_id="",
            author=author,
            content_type=content_type_for(text, bool(attachments)),
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
            attachments=attachments,
            server_name=message.guild.name if message.guild else None,
            channel_name=getattr(message.channel, "name", None),
            trigger=is_trigger,
            reply_to_author=reply_to_author_group,
        )

        compat_delay: float = (
            self._compat.match(message) if message.webhook_id is None else 0.0
        )

        if compat_delay > 0:
            async def _delayed(
                m=message, msg_=msg, ch=message.channel, ck=cursor_key
            ) -> None:
                await asyncio.sleep(compat_delay)
                try:
                    await m.channel.fetch_message(m.id)
                except discord.NotFound:
                    logger.debug(
                        "Discord: message %s deleted (proxy bot) — dropped", m.id
                    )
                    return
                except Exception:
                    pass
                if msg_.trigger:
                    task = asyncio.create_task(
                        _turn_module.handle_turn(self, msg_, ch, ck)
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                else:
                    lock = self._lane_locks.setdefault(ck, asyncio.Lock())
                    async with lock:
                        node_id = self._get_or_create_cursor(ck)
                        new_node_id = await self._runtime.push(
                            dataclasses.replace(msg_, tail_node_id=node_id)
                        )
                        self._advance_cursor(ck, new_node_id)

            task = asyncio.create_task(_delayed())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        if not is_trigger:
            lock = self._lane_locks.setdefault(cursor_key, asyncio.Lock())
            async with lock:
                node_id = self._get_or_create_cursor(cursor_key)
                new_node_id = await self._runtime.push(
                    dataclasses.replace(msg, tail_node_id=node_id)
                )
                self._advance_cursor(cursor_key, new_node_id)
            return

        task = asyncio.create_task(
            _turn_module.handle_turn(self, msg, message.channel, cursor_key)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Thread message handler
    # ------------------------------------------------------------------

    async def _handle_thread_message(self, message: discord.Message) -> None:
        if message.author.bot and (
            self._client.user is None
            or message.author.id == self._client.user.id
        ):
            return

        thread     = message.channel
        thread_id  = str(thread.id)
        channel_id = (
            str(thread.parent_id)
            if isinstance(thread, discord.Thread) and thread.parent_id
            else ""
        )
        cursor_key = f"thread:{thread_id}"

        text        = message.content.strip()
        attachments = await self._fetch_attachments(message)
        fwd_text, fwd_attachments = await self._extract_forwarded(message)
        if fwd_text:
            text = f"{text}\n{fwd_text}".strip()
            attachments = attachments + fwd_attachments
        if not text and not attachments:
            return

        author = self._runtime.users.resolve_user(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.name,
            display_name=message.author.display_name,
        )
        self._get_or_create_thread_cursor(thread_id, channel_id)
        bot_id_thread  = self._client.user.id if self._client.user else None
        ref_thread     = message.reference
        resolved_thread = ref_thread.resolved if ref_thread else None
        reply_to_author_thread: str | None = None
        if (
            isinstance(resolved_thread, discord.Message)
            and resolved_thread.author.id != bot_id_thread
        ):
            resolved_user_thread = self._runtime.users.resolve_user(
                platform=Platform.DISCORD,
                user_id=str(resolved_thread.author.id),
                username=resolved_thread.author.name,
                display_name=resolved_thread.author.display_name,
            )
            reply_to_author_thread = resolved_user_thread.username
        msg = InboundMessage(
            tail_node_id="",
            author=author,
            content_type=content_type_for(text, bool(attachments)),
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
            attachments=attachments,
            server_name=message.guild.name if message.guild else None,
            channel_name=getattr(thread, "name", None),
            trigger=True,
            reply_to_author=reply_to_author_thread,
        )
        task = asyncio.create_task(
            _turn_module.handle_turn(self, msg, message.channel, cursor_key)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        token_env = str(self._opts["token_env"])
        token     = os.environ.pop(token_env, "")
        if not token:
            raise RuntimeError(
                f"Discord bridge: env var '{token_env}' is not set. "
                "Export your bot token before starting."
            )
        logger.info("Discord bridge: starting (token_env=%s)", token_env)
        await self._client.start(token)

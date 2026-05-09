"""
bridges/discord/__main__.py — Discord bridge for TinyCTX.

Uses discord.py (pip install discord.py).

Config (in config.yaml under bridges.discord.options):
  token_env:         Name of the env var holding the bot token.
                     Default: DISCORD_BOT_TOKEN
  allowed_users_dm:  Allowlist of Discord user IDs (integers) permitted to DM
                     the bot. Empty list = open to everyone.
                     Default: []  (WARNING: open access — set this!)
  allowed_servers:   Map of server (guild) ID to a list of allowed channel IDs.
                     Only servers present in this map are served. For each
                     server, an empty channel list means all channels are
                     allowed; a non-empty list restricts to those channels.
                     Default: {}  (WARNING: no servers will be served — set this!)
                     Example:
                       allowed_servers:
                         123456789012345678: []          # all channels
                         987654321098765432:             # specific channels only
                           - 111111111111111111
                           - 222222222222222222
  admin_users:       List of Discord user IDs (integers) permitted to use
                     /reset in group sessions. Empty = nobody can reset.
                     Default: []
  dm_enabled:        Allow DMs to the bot. Default: true
  prefix_required:   In group channels, only respond when @mentioned or when
                     the message starts with the command_prefix.
                     Default: true (ignore messages that don't mention or prefix)
  command_prefix:    Text prefix that triggers the bot in group channels.
                     Default: "!"
  reset_command:     Command string that triggers a session reset in group channels.
                     Default: "/reset"
  buffer_timeout_s:  In group channels, seconds to wait after a non-trigger
                     message before flushing buffered messages anyway.
                     0 = disabled (only flush on trigger). Default: 0
  buffer_head_lines: When truncating a large burst, keep this many messages
                     from the START (topic context). Default: 2
  buffer_tail_lines: Messages to keep from the END of a truncated burst
                     (closest to the trigger). Default: 10
                     Omitted middle is replaced with:
                     "... [N messages not shown] ..."
                     Trigger detection, buffering, and truncation are all
                     handled by GroupLane in router.py via GroupPolicy.
  max_reply_length:  Discord message length cap before chunking. Default: 1900
  typing_indicator:  Show "Bot is typing..." while the agent thinks. Default: true

Compat rules (bridges/discord/compat.json):
  Per-pattern delay rules for proxy-bot compatibility. Each entry specifies a
  match condition and a delay in seconds. When a non-webhook message matches,
  it is held for delay_s then verified via fetch_message — if deleted (proxied),
  it is dropped and the webhook repost is handled instead.

  Match fields (all optional, ANDed together):
    content_regex   — regex tested against message content
    author_id       — exact Discord user ID (integer) of the sender
    has_webhook_id  — if true, only match messages that ARE webhooks

  Example compat.json:
    [
      {
        "description": "Tupperbot proxy",
        "match": { "content_regex": "^\\w+:.+" },
        "delay_s": 0.8
      }
    ]

Thread branching:
  When a Discord thread is created inside a tracked channel, the bot creates a
  new DB branch forked off the channel turn that spawned the thread. The channel
  and thread then evolve independently — both can be active simultaneously. The
  thread agent sees the full channel history up to the fork point, plus whatever
  has happened inside the thread since then.

  Cursor persistence:
  All cursors (DMs, channels, threads) are persisted to
  workspace/cursors/discord.json so sessions survive bot restarts. The file maps
  cursor_key strings to DB node UUIDs:
    "dm:<user_id>"        → node_id
    "group:<channel_id>"  → node_id  (advances with each turn)
    "thread:<thread_id>"  → node_id  (advances with each turn)

  Message → node mapping for fork points:
  When a channel trigger message is processed, the DB node ID of the resulting
  user turn is recorded in workspace/cursors/discord_msg_nodes.json keyed by
  Discord message ID. When a thread is created from that message, its cursor
  is initialised to that node ID — branching the tree exactly at that turn.
  If the origin message isn't mapped (e.g. predates the bot), the thread falls
  back to the channel's current tail.

Slash commands:
  All commands registered via CommandRegistry are automatically registered as
  native Discord application commands (slash commands). They appear in Discord's
  autocomplete UI and are dispatched via Interaction objects, not text parsing.

  Registration happens in two stages:
    1. On bot startup, _sync_app_commands() walks CommandRegistry and builds
       one discord.app_commands.Command per (namespace, sub) entry. Commands
       with a subcommand become "/namespace_sub" (underscore-joined) to stay
       within Discord's single-level command limit. Bare "/namespace" commands
       become "/namespace".
    2. After the tree is built, app_commands.CommandTree.sync() pushes the
       full command list to Discord's API. Sync is global (no guild_ids needed)
       and takes up to 1 hour to propagate to all clients.

  Interaction handling:
    Each command immediately defers the interaction (sends a "thinking..."
    acknowledgement within Discord's 3-second window), calls the handler with
    the standard context dict (plus "interaction" and "followup" keys), then
    calls interaction.followup.send() with the result.

  reset_command is also registered as a native slash command (/reset).
  The text-based "/..." interception in on_message is removed entirely.

Token setup:
  export DISCORD_BOT_TOKEN=your-bot-token-here

Required bot intents (Discord Developer Portal):
  - Message Content Intent (privileged — must be enabled manually)
  - Server Members Intent (optional but helpful for username resolution)

Finding your Discord user ID:
  Enable Developer Mode in Discord (Settings → Advanced → Developer Mode),
  then right-click your username and select "Copy User ID".
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from TinyCTX.contracts import (
    AgentError,
    AgentOutboundFiles,
    AgentThinkingChunk,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    Attachment,
    content_type_for,
    InboundMessage,
    Platform,
    UserIdentity,
)

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
# Mention humanization
# ---------------------------------------------------------------------------

async def _humanize_mentions(text: str, client: discord.Client) -> str:
    """Replace <@id> and <@!id> with @username in text."""
    pattern = re.compile(r"<@!?(\d+)>")

    async def _replace(match: re.Match) -> str:
        try:
            user = await client.fetch_user(int(match.group(1)))
            return f"@{user.name}"
        except Exception:
            return f"@[{match.group(1)}]"

    parts: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(text[last : m.start()])
        parts.append(await _replace(m))
        last = m.end()
    parts.append(text[last:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Compat rules — pattern-based per-message delay for proxy-bot compatibility
# ---------------------------------------------------------------------------

class CompatRules:
    """
    Loads bridges/discord/compat.json and matches incoming messages against
    a list of rules. Each rule specifies match conditions and a delay_s.

    Schema (list of objects):
      {
        "description": "human label (optional)",
        "match": {
          "content_regex":  "regex against message.content",
          "author_id":      12345678,   // exact user ID
          "has_webhook_id": true        // true = message must be a webhook
        },
        "delay_s": 0.8
      }

    All match fields within a rule are ANDed. First matching rule wins.
    """

    def __init__(self, path: Path) -> None:
        self._path  = path
        self._rules: list[dict] = []
        self._mtime: float      = 0.0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._rules = []
            return
        try:
            mtime = self._path.stat().st_mtime
            if mtime == self._mtime:
                return
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            # Pre-compile regexes.
            compiled = []
            for rule in raw:
                entry = dict(rule)
                m = entry.get("match", {})
                if "content_regex" in m:
                    entry["_content_re"] = re.compile(m["content_regex"])
                compiled.append(entry)
            self._rules = compiled
            self._mtime = mtime
            logger.info("Discord compat: loaded %d rule(s) from %s", len(self._rules), self._path)
        except Exception:
            logger.exception("Discord compat: failed to load %s", self._path)

    def match(self, message: discord.Message) -> float:
        """
        Return the delay_s for the first matching rule, or 0.0 if no rule matches.
        Reloads the file if it has changed on disk.
        """
        self._load()
        for rule in self._rules:
            m = rule.get("match", {})
            if "content_regex" in m:
                if not rule["_content_re"].search(message.content):
                    continue
            if "author_id" in m:
                if message.author.id != int(m["author_id"]):
                    continue
            if "has_webhook_id" in m:
                want = bool(m["has_webhook_id"])
                if bool(message.webhook_id) != want:
                    continue
            return float(rule.get("delay_s", 0.0))
        return 0.0


# ---------------------------------------------------------------------------
# Cursor store — persists all Discord cursors across restarts
# ---------------------------------------------------------------------------

class CursorStore:
    """
    Persists two JSON files under workspace/cursors/:

    discord.json          — cursor_key -> node_id
                            Keys: "dm:<uid>", "group:<cid>", "thread:<tid>"

    discord_msg_nodes.json — discord_message_id -> db_node_id
                            Records which DB node a channel trigger message
                            produced, so thread forks can branch accurately.
                            Capped at MAX_MSG_NODES entries (LRU-style trim).
    """

    MAX_MSG_NODES = 2000

    def __init__(self, cursors_dir: Path) -> None:
        self._dir           = cursors_dir
        self._cursor_file   = cursors_dir / "discord.json"
        self._msg_node_file = cursors_dir / "discord_msg_nodes.json"
        self._cursors:   dict[str, str] = self._load(self._cursor_file)
        self._msg_nodes: dict[str, str] = self._load(self._msg_node_file)

    # ------------------------------------------------------------------
    # Cursor map (cursor_key -> node_id)
    # ------------------------------------------------------------------

    def get(self, cursor_key: str) -> str | None:
        return self._cursors.get(cursor_key)

    def set(self, cursor_key: str, node_id: str) -> None:
        self._cursors[cursor_key] = node_id
        self._save(self._cursor_file, self._cursors)

    def delete(self, cursor_key: str) -> None:
        self._cursors.pop(cursor_key, None)
        self._save(self._cursor_file, self._cursors)

    def all_cursors(self) -> dict[str, str]:
        return dict(self._cursors)

    # ------------------------------------------------------------------
    # Message → node map (discord_message_id -> db_node_id)
    # ------------------------------------------------------------------

    def get_msg_node(self, discord_message_id: str) -> str | None:
        return self._msg_nodes.get(discord_message_id)

    def set_msg_node(self, discord_message_id: str, node_id: str) -> None:
        self._msg_nodes[discord_message_id] = node_id
        # Trim to cap if needed (remove oldest entries)
        if len(self._msg_nodes) > self.MAX_MSG_NODES:
            overflow = len(self._msg_nodes) - self.MAX_MSG_NODES
            for key in list(self._msg_nodes.keys())[:overflow]:
                del self._msg_nodes[key]
        self._save(self._msg_node_file, self._msg_nodes)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _load(path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("CursorStore: corrupt file %s — starting fresh", path)
        return {}

    @staticmethod
    def _save(path: Path, data: dict) -> None:
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("CursorStore: failed to save %s", path)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _make_session_node(db, cursor_key: str) -> str:
    """Create a new session-anchor node off the global root and return its id."""
    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content=f"session:{cursor_key}")
    return node.id


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class DiscordBridge:
    def __init__(self, runtime: "Runtime", options: dict) -> None:
        self._runtime = runtime
        self._opts    = {**DEFAULTS, **options}

        self._max_len:          int   = int(self._opts["max_reply_length"])
        self._typing:           bool  = bool(self._opts["typing_indicator"])
        self._typing_on_thinking: bool = bool(self._opts["typing_on_thinking"])
        self._typing_on_tools:  bool  = bool(self._opts["typing_on_tools"])
        self._typing_on_reply:  bool  = bool(self._opts["typing_on_reply"])
        self._prefix:           str   = str(self._opts["command_prefix"])
        self._prefix_required:  bool  = bool(self._opts["prefix_required"])
        self._reset_command:    str   = str(self._opts["reset_command"])
        self._shutdown_command: str   = str(self._opts["shutdown_command"])
        self._dm_enabled:       bool  = bool(self._opts["dm_enabled"])

        # allowed_servers: {guild_id: set[channel_id]} — empty set = all channels
        raw_servers = self._opts["allowed_servers"]
        self._allowed_servers: dict[int, set[int]] = {
            int(guild_id): {int(c) for c in channels}
            for guild_id, channels in raw_servers.items()
        }
        self._buffer_timeout_s:  float = float(self._opts["buffer_timeout_s"])
        self._buffer_head_lines: int   = int(self._opts["buffer_head_lines"])
        self._buffer_tail_lines: int   = int(self._opts["buffer_tail_lines"])

        self._allowed_users_dm: set[int] = {int(u) for u in self._opts["allowed_users_dm"]}
        self._admin_users:   set[int] = {int(u) for u in self._opts["admin_users"]}

        # In-flight state (not persisted)
        self._typing_active: dict[str, asyncio.Event]     = {}
        self._tasks:         set[asyncio.Task]            = set()  # strong refs, prevent GC
        # Maps cursor_key -> active channel, so handle_event can route AgentOutboundFiles.
        self._active_channels: dict[str, discord.abc.Messageable] = {}
        # Maps node_id -> cursor_key for any turn currently in-flight.
        # Populated when push() returns the user node id; cleared when the turn ends.
        self._node_to_cursor:  dict[str, str] = {}
        # Monotonically-increasing reset counter per cursor_key.
        # _handle_turn checks this so a post-reset turn can't re-advance
        # the cursor after it has been rewound by /reset.
        self._reset_epoch:   dict[str, int]               = {}
        # Per-lane asyncio.Lock — serialises concurrent _handle_turn calls on the same cursor_key.
        self._lane_locks:    dict[str, asyncio.Lock]      = {}

        # Persisted cursor store
        workspace   = Path(runtime.config.workspace.path).expanduser().resolve()
        cursors_dir = workspace / "cursors"
        cursors_dir.mkdir(parents=True, exist_ok=True)
        self._store = CursorStore(cursors_dir)

        # Compat rules — loaded from bridges/discord/compat.json
        _bridge_dir  = Path(__file__).parent
        self._compat = CompatRules(_bridge_dir / "compat.json")

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members         = True
        # Use Bot instead of Client so we can attach an app_commands tree.
        # command_prefix is set but unused — we rely on slash commands only.
        self._client = discord_commands.Bot(command_prefix="\\", intents=intents)
        self._tree   = self._client.tree

        # Assign directly by name (same reason as before — avoid __name__ prefix).
        self._client.on_ready   = self._on_ready
        self._client.on_message = self._on_message

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    def _get_cursor(self, cursor_key: str) -> str | None:
        return self._store.get(cursor_key)

    def _get_or_create_cursor(self, cursor_key: str) -> str:
        node_id = self._store.get(cursor_key)
        if not node_id:
            node_id = _make_session_node(self._runtime.db, cursor_key)
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
                node_id = _make_session_node(self._runtime.db, cursor_key)
                logger.info("Discord: thread %s no parent — fresh branch %s", thread_id, node_id)
            else:
                node_id = parent_node_id
                logger.info("Discord: thread %s forked from node %s", thread_id, parent_node_id)
            self._store.set(cursor_key, node_id)
        return node_id

    def _advance_cursor(self, cursor_key: str, new_tail: str) -> None:
        """Persist the new tail for this cursor."""
        self._store.set(cursor_key, new_tail)
        logger.info("Discord: cursor %s advanced to %s", cursor_key, new_tail)

    # ------------------------------------------------------------------
    # App command sync
    # ------------------------------------------------------------------

    async def _sync_app_commands(self) -> None:
        """
        Walk CommandRegistry and register commands as native Discord slash commands.

        Commands with a subcommand (e.g. "/memory consolidate") are registered
        as proper Discord subcommands under an app_commands.Group, so they appear
        in Discord's UI as "/memory" → select "consolidate".

        Bare commands (e.g. "/heartbeat run" where there's only one sub, or a
        top-level "/namespace" with no sub) are registered as flat slash commands.

        /reset is always registered directly from bridge config.
        """
        self._tree.clear_commands(guild=None)

        # Register /reset.
        reset_cmd_name = self._reset_command.lstrip("/").replace(" ", "_")

        @self._tree.command(name=reset_cmd_name, description="Reset the current session")
        async def _reset_slash(interaction: discord.Interaction) -> None:
            await self._handle_reset_interaction(interaction)

        # Register /shutdown.
        shutdown_cmd_name = self._shutdown_command.lstrip("/").replace(" ", "_")

        @self._tree.command(name=shutdown_cmd_name, description="Kill the TinyCTX gateway (admin only)")
        async def _shutdown_slash(interaction: discord.Interaction) -> None:
            await self._handle_shutdown_interaction(interaction)

        # Group commands by namespace so we can decide flat vs subcommand.
        # namespace -> {sub -> (help_text, ns, sub)}
        grouped: dict[str, dict[str, tuple[str, str, str]]] = {}
        for cmd_str, help_text in self._runtime.commands.list_commands():
            parts     = cmd_str.lstrip("/").split()
            namespace = parts[0]
            sub       = parts[1] if len(parts) > 1 else ""
            grouped.setdefault(namespace, {})[sub] = (help_text or f"Run {cmd_str}", namespace, sub)

        for namespace, subs in grouped.items():  # type: ignore[assignment]
            # If there's only a bare entry (no sub) or only one sub with no bare,
            # register as a flat command to keep things simple.
            has_bare = "" in subs
            named_subs = {k: v for k, v in subs.items() if k}

            if not named_subs:
                # Bare namespace only — flat command.
                desc, ns, sub = subs[""]
                def _make_flat(ns: str, sub: str):  # noqa: E306
                    @self._tree.command(name=ns, description=desc)
                    async def _handler(interaction: discord.Interaction) -> None:
                        await self._handle_command_interaction(interaction, ns, sub)
                _make_flat(ns, sub)
                continue

            # Has named subcommands — use a Group.
            group = app_commands.Group(name=namespace, description=f"{namespace} commands")

            for sub_name, (desc, ns, sub) in named_subs.items():
                def _make_sub(ns: str, sub: str, desc: str):
                    @group.command(name=sub, description=desc)
                    async def _sub_handler(interaction: discord.Interaction) -> None:
                        await self._handle_command_interaction(interaction, ns, sub)
                _make_sub(ns, sub_name, desc)

            # If there's also a bare entry, add it as a subcommand named "run".
            if has_bare:
                desc, ns, sub = subs[""]
                def _make_bare_as_run(ns: str, desc: str):
                    @group.command(name="run", description=desc)
                    async def _run_handler(interaction: discord.Interaction) -> None:
                        await self._handle_command_interaction(interaction, ns, "")
                _make_bare_as_run(ns, desc)

            self._tree.add_command(group)

        try:
            synced = await self._tree.sync()
            logger.info(
                "Discord bridge: synced %d app command(s) to Discord", len(synced)
            )
        except Exception:
            logger.exception("Discord bridge: failed to sync app commands")

    async def _handle_reset_interaction(self, interaction: discord.Interaction) -> None:
        """Handle the /reset slash command."""
        channel = interaction.channel
        is_dm   = isinstance(channel, discord.DMChannel)
        await interaction.response.defer(ephemeral=False)
        user_id = interaction.user.id

        if not is_dm and not self._is_admin(user_id):
            await interaction.followup.send("⛔ Only admins can reset the session.", ephemeral=True)
            return

        # Determine cursor_key for this context.
        if is_dm:
            cursor_key = f"dm:{interaction.user.id}"
        elif isinstance(channel, discord.Thread):
            cursor_key = f"thread:{channel.id}"
        else:
            cursor_key = f"group:{channel.id}" if channel else None

        if cursor_key:
            self._reset_epoch[cursor_key] = self._reset_epoch.get(cursor_key, 0) + 1
            new_node_id = _make_session_node(self._runtime.db, cursor_key)
            self._store.set(cursor_key, new_node_id)
            logger.info(
                "Discord: session reset by %s — new branch %s for %s",
                interaction.user.id, new_node_id, cursor_key,
            )
        await interaction.followup.send("✅ Session reset.", ephemeral=True)

    async def _handle_shutdown_interaction(self, interaction: discord.Interaction) -> None:
        """Handle the /shutdown slash command — kills the gateway process."""
        await interaction.response.defer(ephemeral=True)

        if not self._is_admin(interaction.user.id):
            await interaction.followup.send(
                "⛔ Only admins can shut down the gateway.", ephemeral=True
            )
            return

        import urllib.request
        import urllib.error

        cfg         = self._runtime.config
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
            # Connection reset/abort is expected — the server died mid-response.
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                await interaction.followup.send("🔴 Gateway is shutting down.", ephemeral=True)
            else:
                await interaction.followup.send(
                    f"⚠️ Shutdown failed: {exc}", ephemeral=True
                )

    async def _handle_command_interaction(
        self,
        interaction: discord.Interaction,
        namespace: str,
        sub: str,
    ) -> None:
        """Dispatch a registered CommandRegistry command via a native interaction."""
        # Defer immediately — handlers can take longer than 3 seconds.
        await interaction.response.defer(ephemeral=False)

        # Build context dict matching what text-based dispatch used to pass,
        # plus interaction-specific keys for handlers that want them.
        channel = interaction.channel
        if isinstance(channel, discord.DMChannel):
            cursor_key = f"dm:{interaction.user.id}"
        elif isinstance(channel, discord.Thread):
            cursor_key = f"thread:{channel.id}"
        else:
            cursor_key = f"group:{channel.id}" if channel else None

        node_id = self._store.get(cursor_key) or "" if cursor_key else ""

        reply_parts: list[str] = []

        async def _send_reply(text: str) -> None:
            reply_parts.append(text)

        ctx = {
            "channel":     channel,
            "interaction": interaction,
            "followup":    interaction.followup,
            "guild":       interaction.guild,
            "bridge":      self,
            "runtime":     self._runtime,
            "cursor":      node_id,
            "send":        _send_reply,
        }

        text = f"/{namespace} {sub}".strip() if sub else f"/{namespace}"
        handled = await self._runtime.commands.dispatch(text, ctx)

        if not handled:
            await interaction.followup.send("⚠️ Command not found.", ephemeral=True)
            return

        # If the handler accumulated reply parts via ctx["send"], send them.
        if reply_parts:
            combined = "\n".join(reply_parts)
            for i in range(0, len(combined), self._max_len):
                await interaction.followup.send(combined[i : i + self._max_len])
        # If the handler sent nothing, send a silent acknowledgement.
        else:
            await interaction.followup.send("✅ Done.", ephemeral=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_permission_level(self, member_roles: list | None) -> int:
        """
        Resolve a Discord sender's permission level (0-100) from their roles.

        Keyed by role ID (integer) to prevent spoofing via role renames.
        Iterates roles highest-position first, returns the first mapped level.
        Falls back to default_permission if no role matches.

        Config (under bridges.discord.options):
          default_permission: 25
          role_permissions:
            123456789012345678: 100   # Admin role ID
            234567890123456789: 50    # Moderator role ID
            345678901234567890: 25    # Member role ID
        """
        role_map = self._opts.get("role_permissions", {})
        default  = int(self._opts.get("default_permission", 25))
        if not member_roles or not role_map:
            return default
        # Normalise keys to int so YAML integer keys and string keys both work.
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
        """Return True if the guild is in allowed_servers and the channel is permitted."""
        if guild_id not in self._allowed_servers:
            return False
        allowed_channels = self._allowed_servers[guild_id]
        # Empty set means all channels in that server are allowed.
        return not allowed_channels or channel_id in allowed_channels

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admin_users

    def _is_group_trigger(self, text: str) -> bool:
        """Return True if this message should trigger an agent response."""
        if not self._prefix_required:
            return True
        if text.startswith(self._prefix):
            return True
        bot_name = self._client.user.name if self._client.user else ""
        if bot_name and f"@{bot_name}" in text:
            return True
        return False

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
                fetched.append(Attachment(filename=a.filename, data=data, mime_type=mime))
            except Exception:
                logger.warning("Discord: failed to download attachment %s", a.filename)
        return tuple(fetched)

    # ------------------------------------------------------------------
    # Event handler registered with Runtime (kept for AgentOutboundFiles
    # which is fired outside the normal turn queue by the present() tool)
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:
        if isinstance(event, AgentOutboundFiles):
            # AgentOutboundFiles is fired as a detached task from within the
            # tool — it never goes through the per-turn queue. Route by looking
            # up which cursor_key is currently active for this node.
            cursor_key = self._node_to_cursor.get(event.tail_node_id)
            channel    = self._active_channels.get(cursor_key) if cursor_key else None
            if channel is None:
                logger.warning("AgentOutboundFiles but no active channel for tail %s", event.tail_node_id)
                return
            for path in event.paths:
                try:
                    await channel.send(file=discord.File(path))
                except Exception as exc:
                    logger.warning("Discord: failed to upload file %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Discord callbacks
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
                "to DMs from anyone. Set bridges.discord.options.allowed_users_dm in config.yaml."
            )
        if not self._allowed_servers:
            logger.warning(
                "Discord bridge: allowed_servers is empty — the bot will not respond "
                "in any server. Set bridges.discord.options.allowed_servers in config.yaml."
            )
        if not self._admin_users:
            logger.warning(
                "Discord bridge: admin_users is empty — nobody can use /%s in group sessions.",
                self._reset_command.lstrip("/"),
            )
        await self._sync_app_commands()

    async def _on_message(self, message: discord.Message) -> None:
        # Ignore our own messages — use message.author.bot as a fallback
        # for the edge case where self._client.user is None during startup.
        if message.author.bot and (
            self._client.user is None
            or message.author.id == self._client.user.id
        ):
            return

        # Per-context access checks happen below (DM vs group).

        # ── Thread message ────────────────────────────────────────────
        if isinstance(message.channel, discord.Thread):
            await self._handle_thread_message(message)
            return

        # ── DM ────────────────────────────────────────────────────────
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
            if not text and not attachments:
                return

            cursor_key = f"dm:{message.author.id}"
            author     = UserIdentity(
                platform=Platform.DISCORD,
                user_id=str(message.author.id),
                username=message.author.name,
            )
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
                permission_level=self._opts.get("dm_permission", 25),
                trigger=True,
            )
            task = asyncio.create_task(
                self._handle_turn(msg, message.channel, cursor_key)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        # ── Group channel ─────────────────────────────────────────────
        # Trigger detection, mention stripping, buffering, and truncation are
        # all handled by GroupLane in router.py via the GroupPolicy we attach.
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

        bot_id      = self._client.user.id if self._client.user else None
        text        = await _humanize_mentions(raw_text, self._client)
        attachments = await self._fetch_attachments(message)

        bot_name = self._client.user.name if self._client.user else None
        if bot_id and bot_name:
            ref = message.reference
            resolved = ref.resolved if ref else None
            if (
                isinstance(resolved, discord.Message)
                and resolved.author.id == bot_id
                and f"@{bot_name}" not in text
            ):
                text = f"@{bot_name} {text}"
        if not text and not attachments:
            return

        author       = UserIdentity(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.name,
        )
        member_roles = getattr(message.author, "roles", None)
        is_trigger   = self._is_group_trigger(text)
        msg = InboundMessage(
            tail_node_id="",
            author=author,
            content_type=content_type_for(text, bool(attachments)),
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
            attachments=attachments,
            server_name=message.guild.name if message.guild else None,
            channel_name=message.channel.name if hasattr(message.channel, "name") else None,
            permission_level=self._resolve_permission_level(member_roles),
            trigger=is_trigger,
        )

        compat_delay: float = self._compat.match(message) if message.webhook_id is None else 0.0

        if compat_delay > 0:
            async def _delayed(m=message, msg_=msg, ch=message.channel, ck=cursor_key) -> None:
                await asyncio.sleep(compat_delay)
                try:
                    await m.channel.fetch_message(m.id)
                except discord.NotFound:
                    logger.debug(
                        "Discord: message %s deleted (proxy bot) — dropped", m.id,
                    )
                    return
                except Exception:
                    pass
                if msg_.trigger:
                    task = asyncio.create_task(self._handle_turn(msg_, ch, ck))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                else:
                    node_id = self._get_or_create_cursor(ck)
                    new_node_id = await self._runtime.push(dataclasses.replace(msg_, tail_node_id=node_id))
                    self._advance_cursor(ck, new_node_id)
            task = asyncio.create_task(_delayed())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        if not is_trigger:
            lock = self._lane_locks.setdefault(cursor_key, asyncio.Lock())
            async with lock:
                node_id = self._get_or_create_cursor(cursor_key)
                new_node_id = await self._runtime.push(dataclasses.replace(msg, tail_node_id=node_id))
                self._advance_cursor(cursor_key, new_node_id)
            return

        task = asyncio.create_task(
            self._handle_turn(msg, message.channel, cursor_key)
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
        channel_id = str(thread.parent_id) if thread.parent_id else ""
        cursor_key = f"thread:{thread_id}"

        text        = message.content.strip()
        attachments = await self._fetch_attachments(message)
        if not text and not attachments:
            return

        author  = UserIdentity(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.name,
        )
        member_roles = getattr(message.author, "roles", None)
        # Ensure the thread cursor exists (fork logic lives in _get_or_create_thread_cursor)
        # but don't snapshot it — _handle_turn reads it under the lock.
        self._get_or_create_thread_cursor(thread_id, channel_id)
        msg = InboundMessage(
            tail_node_id="",
            author=author,
            content_type=content_type_for(text, bool(attachments)),
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
            attachments=attachments,
            server_name=message.guild.name if message.guild else None,
            channel_name=thread.name,
            permission_level=self._resolve_permission_level(member_roles),
            trigger=True,
        )
        task = asyncio.create_task(
            self._handle_turn(msg, message.channel, cursor_key)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Turn handling
    # ------------------------------------------------------------------

    async def _typing_keepalive(
        self,
        channel: discord.abc.Messageable,
        active_event: asyncio.Event,
        done_event: asyncio.Event,
    ) -> None:
        await active_event.wait()
        while not done_event.is_set():
            try:
                async with channel.typing():
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=8.0)
                    except asyncio.TimeoutError:
                        pass
            except Exception:
                await asyncio.sleep(1)

    async def _handle_turn(
        self,
        msg: InboundMessage,
        channel: discord.abc.Messageable,
        cursor_key: str,
    ) -> None:
        """Execute one agent turn, serialised per cursor_key via _lane_locks."""
        epoch_at_start = self._reset_epoch.get(cursor_key, 0)
        lock = self._lane_locks.setdefault(cursor_key, asyncio.Lock())

        async with lock:
            # Read live tail under the lock — no snapshot race.
            node_id = self._get_or_create_cursor(cursor_key)
            msg = dataclasses.replace(msg, tail_node_id=node_id)

            self._active_channels[cursor_key] = channel

            done_event   = asyncio.Event()
            typing_ev    = asyncio.Event()
            reply_queue: asyncio.Queue = asyncio.Queue()

            if self._typing:
                keepalive = asyncio.create_task(
                    self._typing_keepalive(channel, typing_ev, done_event)
                )

            new_tail: str | None = None
            try:
                new_tail = await self._runtime.push(msg, reply_queue=reply_queue)
                # Persist user node tail immediately.
                self._advance_cursor(cursor_key, new_tail)
                # Register node→cursor so AgentOutboundFiles can find the channel.
                self._node_to_cursor[new_tail] = cursor_key

                if not msg.trigger:
                    return

                turn_timeout: float | None = float(self._opts.get("turn_timeout_s", 0)) or None
                buf: list[str] = []

                while True:
                    try:
                        event = await asyncio.wait_for(
                            reply_queue.get(),
                            timeout=turn_timeout,
                        )
                    except asyncio.TimeoutError:
                        await channel.send("⚠️ Response timed out.")
                        break

                    if event is None:  # sentinel: turn complete
                        break

                    if isinstance(event, AgentTextChunk):
                        if self._typing_on_reply:
                            typing_ev.set()
                        buf.append(event.text)
                    elif isinstance(event, AgentThinkingChunk):
                        if self._typing_on_thinking:
                            typing_ev.set()
                    elif isinstance(event, AgentTextFinal):
                        if event.text:
                            buf.append(event.text)
                        # Advance cursor to real assistant tail.
                        current_epoch = self._reset_epoch.get(cursor_key, 0)
                        if current_epoch == epoch_at_start and event.tail_node_id:
                            self._advance_cursor(cursor_key, event.tail_node_id)
                    elif isinstance(event, AgentToolCall):
                        if self._typing_on_tools:
                            typing_ev.set()
                        logger.debug("Discord: tool call %s for %s", event.tool_name, cursor_key)
                    elif isinstance(event, AgentToolResult):
                        logger.debug(
                            "Discord: tool result %s (%s) for %s",
                            event.tool_name, "error" if event.is_error else "ok", cursor_key,
                        )
                    elif isinstance(event, AgentError):
                        await channel.send(f"⚠️ {event.message}")
                        break

                # Send accumulated text.
                text = "".join(buf).strip()
                if text:
                    for i in range(0, len(text), self._max_len):
                        await channel.send(text[i : i + self._max_len])

            except Exception:
                logger.exception("Discord: error handling turn for %s", cursor_key)
            finally:
                done_event.set()
                typing_ev.set()
                self._active_channels.pop(cursor_key, None)
                self._node_to_cursor.pop(new_tail, None) if new_tail else None
                if self._typing:
                    keepalive.cancel()
                    try:
                        await keepalive
                    except asyncio.CancelledError:
                        pass

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

        self._runtime.register_platform_handler(Platform.DISCORD.value, self.handle_event)
        logger.info("Discord bridge: starting (token_env=%s)", token_env)
        await self._client.start(token)


# ---------------------------------------------------------------------------
# Loader entrypoint (called by main.py)
# ---------------------------------------------------------------------------

async def run(runtime: "Runtime") -> None:
    """Entry point called by main.py bridge loader."""
    bridge_cfg = runtime.config.bridges.get("discord")
    options: dict = bridge_cfg.options if bridge_cfg else {}
    bridge = DiscordBridge(runtime, options)
    await bridge.run()

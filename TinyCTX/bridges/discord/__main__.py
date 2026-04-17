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
    ActivationMode,
    AgentError,
    AgentOutboundFiles,
    AgentThinkingChunk,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    Attachment,
    content_type_for,
    GroupPolicy,
    InboundMessage,
    Platform,
    UserIdentity,
)

if TYPE_CHECKING:
    from TinyCTX.router import Router

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
# Reply accumulator
# ---------------------------------------------------------------------------

class _ReplyAccumulator:
    """Accumulates streamed agent text and flushes to a Discord channel."""

    def __init__(self, channel: discord.abc.Messageable, max_len: int) -> None:
        self._channel = channel
        self._max_len = max_len
        self._buf: list[str] = []
        self._done = asyncio.Event()
        self._error: str | None = None

    def feed(self, chunk: str) -> None:
        self._buf.append(chunk)

    def finish(self, final_text: str) -> None:
        if final_text and not self._buf:
            self._buf.append(final_text)
        self._done.set()

    def error(self, message: str) -> None:
        self._error = message
        self._done.set()

    @property
    def channel(self) -> discord.abc.Messageable:
        return self._channel

    async def wait_and_send(self, timeout: float | None = None) -> None:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._error = "Agent response timed out."
        if self._error:
            await self._channel.send(f"⚠️ {self._error}")
            return
        text = "".join(self._buf).strip()
        if not text:
            return
        for i in range(0, len(text), self._max_len):
            await self._channel.send(text[i : i + self._max_len])


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _open_db(router):
    from TinyCTX.db import ConversationDB
    workspace = Path(router._config.workspace.path).expanduser().resolve()
    return ConversationDB(workspace / "agent.db")


def _make_session_node(db, cursor_key: str) -> str:
    """Create a new session-anchor node off the global root and return its id."""
    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content=f"session:{cursor_key}")
    return node.id


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class DiscordBridge:
    def __init__(self, router: "Router", options: dict) -> None:
        self._router = router
        self._opts   = {**DEFAULTS, **options}

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
        self._accumulators:  dict[str, _ReplyAccumulator] = {}
        self._typing_active: dict[str, asyncio.Event]     = {}
        self._tasks:         set[asyncio.Task]            = set()  # strong refs, prevent GC
        # Monotonically-increasing reset counter per cursor_key.
        # _advance_cursor checks this so a post-reset turn can't re-advance
        # the cursor after it has been rewound by /reset.
        self._reset_epoch:   dict[str, int]               = {}
        # Per-lane asyncio.Lock — serialises concurrent _handle_turn calls that
        # would otherwise race on the same node_id key in _accumulators.
        self._lane_locks:    dict[str, asyncio.Lock]      = {}
        # Maps cursor_key -> original lane node_id (never advances after creation).
        # Used by reset so we look up the lane by its stable key, and so we
        # can rewind the persisted cursor back to the session anchor.
        self._lane_keys: dict[str, str] = {}

        # Persisted cursor store
        workspace   = Path(router._config.workspace.path).expanduser().resolve()
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
        # If we have a live lane anchor for this key, always return it.
        # This ensures every message in a session routes to the same persistent
        # lane regardless of what _advance_cursor has written to disk.
        live = self._lane_keys.get(cursor_key)
        if live:
            return live
        # No live anchor: first message this session (or post-restart).
        # Read the persisted cursor (may be a tail snapshot from a previous run)
        # and use it as the new anchor.
        node_id = self._store.get(cursor_key)
        if not node_id:
            db      = _open_db(self._router)
            node_id = _make_session_node(db, cursor_key)
            self._store.set(cursor_key, node_id)
            logger.info("Discord: created cursor %s -> %s", cursor_key, node_id)
        self._lane_keys[cursor_key] = node_id
        return node_id

    def _get_or_create_thread_cursor(self, thread_id: str, channel_id: str) -> str:
        """
        Return the cursor node_id for a thread, creating it if necessary.

        Fork logic:
          1. If the thread already has a persisted cursor, use it.
          2. Look up the Discord message that spawned the thread
             (thread.id == starter message id in Discord) in our msg->node map.
             If found, fork off that specific user-turn node — the thread
             inherits full channel history up to that moment.
          3. Fall back to the channel's current tail if no mapping exists.
          4. Fall back to a fresh root-anchored session if the channel has
             no cursor at all (e.g. bot never saw that channel).
        """
        cursor_key = f"thread:{thread_id}"
        # Return live anchor if we have one.
        live = self._lane_keys.get(cursor_key)
        if live:
            return live
        node_id = self._store.get(cursor_key)
        if not node_id:
            # Try to fork from the specific message that created the thread.
            # In Discord, thread.id == id of the starter message.
            parent_node_id = self._store.get_msg_node(thread_id)
            if parent_node_id is None:
                parent_node_id = self._store.get(f"group:{channel_id}")
            if parent_node_id is None:
                db      = _open_db(self._router)
                node_id = _make_session_node(db, cursor_key)
                logger.info("Discord: thread %s no parent — fresh branch %s", thread_id, node_id)
            else:
                node_id = parent_node_id
                logger.info("Discord: thread %s forked from node %s", thread_id, parent_node_id)
            self._store.set(cursor_key, node_id)
        self._lane_keys[cursor_key] = node_id
        return node_id

    def _advance_cursor(self, cursor_key: str, router_node_id: str) -> None:
        """Snapshot the lane's current tail for restart recovery. Does NOT affect
        which node_id is used for the next message — _lane_keys handles that."""
        lane = self._router._lane_router._lanes.get(router_node_id)
        if lane:
            tail = lane.loop._tail_node_id
            if tail and tail != self._store.get(cursor_key):
                self._store.set(cursor_key, tail)
                logger.debug(
                    "Discord: cursor %s tail snapshot %s (for restart recovery)",
                    cursor_key, tail,
                )

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
        for cmd_str, help_text in self._router.commands.list_commands():
            parts     = cmd_str.lstrip("/").split()
            namespace = parts[0]
            sub       = parts[1] if len(parts) > 1 else ""
            grouped.setdefault(namespace, {})[sub] = (help_text or f"Run {cmd_str}", namespace, sub)

        for namespace, subs in grouped.items():
            # If there's only a bare entry (no sub) or only one sub with no bare,
            # register as a flat command to keep things simple.
            has_bare = "" in subs
            named_subs = {k: v for k, v in subs.items() if k}

            if not named_subs:
                # Bare namespace only — flat command.
                desc, ns, sub = subs[""]
                def _make_flat(ns: str, sub: str):
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
            # Bump the epoch FIRST so any in-flight _handle_turn that finishes
            # after this point will see a stale epoch and skip _advance_cursor,
            # keeping the cursor rewound rather than re-advancing it.
            self._reset_epoch[cursor_key] = self._reset_epoch.get(cursor_key, 0) + 1

            lane_node_id = self._lane_keys.get(cursor_key)
            if lane_node_id:
                # Live lane exists — reset it and rewind the persisted cursor
                # back to the session anchor.
                self._router.reset_lane(lane_node_id)
                self._store.set(cursor_key, lane_node_id)
                logger.info(
                    "Discord: session reset via /reset by %s — cursor rewound to %s",
                    interaction.user.id, lane_node_id,
                )
            else:
                # No live lane (bot just restarted or no messages sent yet) —
                # wipe the persisted cursor so the next message starts fresh.
                self._store.delete(cursor_key)
                logger.info(
                    "Discord: session reset via /reset by %s — no live lane, cursor deleted for %s",
                    interaction.user.id, cursor_key,
                )
            # Clear lane_keys so the next message rebuilds from scratch.
            self._lane_keys.pop(cursor_key, None)
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

        cfg       = self._router._config
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

        node_id = self._get_or_create_cursor(cursor_key) if cursor_key else ""

        reply_parts: list[str] = []

        async def _send_reply(text: str) -> None:
            reply_parts.append(text)

        ctx = {
            "channel":     channel,
            "interaction": interaction,
            "followup":    interaction.followup,
            "guild":       interaction.guild,
            "bridge":      self,
            "router":      self._router,
            "cursor":      node_id,
            "send":        _send_reply,   # handlers call ctx["send"](text)
        }

        # Build the text form and dispatch through the registry.
        text = f"/{namespace} {sub}".strip() if sub else f"/{namespace}"
        handled = await self._router.commands.dispatch(text, ctx)

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

    def _is_group_trigger(self, text: str, policy: GroupPolicy) -> bool:
        """Mirror of GroupLane._is_trigger — used to skip accumulator creation
        for non-trigger messages that GroupLane will just buffer."""
        if policy.activation == ActivationMode.ALWAYS:
            return True
        if text.startswith(policy.trigger_prefix):
            return True
        if policy.bot_mxid and policy.bot_mxid in text:
            return True
        if policy.bot_localpart and f"@{policy.bot_localpart}" in text:
            return True
        return False

    def _build_group_policy(self) -> GroupPolicy:
        """Build the GroupPolicy for this channel from bridge config."""
        activation = ActivationMode.ALWAYS if not self._prefix_required else ActivationMode.MENTION
        # Mentions are humanized to "@username" before trigger detection, so
        # match against the humanized form rather than the raw <@id> snowflake.
        bot_name   = self._client.user.name if self._client.user else ""
        return GroupPolicy(
            activation=activation,
            trigger_prefix=self._prefix,
            bot_mxid=f"@{bot_name}",   # matches humanized @mention
            bot_localpart="",           # no legacy form needed after humanization
            buffer_timeout_s=self._buffer_timeout_s,
            buffer_head_lines=self._buffer_head_lines,
            buffer_tail_lines=self._buffer_tail_lines,
        )

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
    # Event handler registered with Router
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:
        node_id = event.lane_node_id   # stable lane key — never advances during a turn
        acc     = self._accumulators.get(node_id)

        # AgentOutboundFiles is fired outside the normal turn flow — the
        # accumulator may or may not be present. Look up the channel directly.
        if isinstance(event, AgentOutboundFiles):
            channel = acc.channel if acc is not None else None
            if channel is None:
                raise RuntimeError(
                    f"AgentOutboundFiles for lane {node_id} but no active channel"
                )
            failed: list[str] = []
            for path in event.paths:
                try:
                    await channel.send(file=discord.File(path))
                except Exception as exc:
                    logger.warning("Discord: failed to upload file %s: %s", path, exc)
                    failed.append(Path(path).name)
            if failed:
                raise RuntimeError(f"Failed to upload: {', '.join(failed)}")
            return

        if acc is None:
            logger.debug("Discord: received event for unknown cursor %s", node_id)
            return

        typing_ev = self._typing_active.get(node_id)

        if isinstance(event, AgentThinkingChunk):
            if typing_ev and self._typing_on_thinking:
                typing_ev.set()
        elif isinstance(event, AgentTextChunk):
            if typing_ev and self._typing_on_reply:
                typing_ev.set()
            acc.feed(event.text)
        elif isinstance(event, AgentTextFinal):
            acc.finish(event.text)
        elif isinstance(event, AgentToolCall):
            if typing_ev and self._typing_on_tools:
                typing_ev.set()
            logger.debug("Discord: tool call %s for cursor %s", event.tool_name, node_id)
        elif isinstance(event, AgentToolResult):
            logger.debug(
                "Discord: tool result %s (%s) for cursor %s",
                event.tool_name, "error" if event.is_error else "ok", node_id,
            )
        elif isinstance(event, AgentError):
            acc.error(event.message)

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
            node_id    = self._get_or_create_cursor(cursor_key)
            author     = UserIdentity(
                platform=Platform.DISCORD,
                user_id=str(message.author.id),
                username=message.author.name,
            )
            msg = InboundMessage(
                tail_node_id=node_id,
                author=author,
                content_type=content_type_for(text, bool(attachments)),
                text=text,
                message_id=str(message.id),
                timestamp=time.time(),
                attachments=attachments,
                # DMs have no server or channel name.
                server_name=None,
                channel_name=None,
                permission_level=self._opts.get("dm_permission", 25),
            )
            acc = _ReplyAccumulator(message.channel, self._max_len)
            task = asyncio.create_task(
                self._handle_turn(msg, message.channel, node_id, acc, cursor_key)
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

        # Treat a Reply-to-bot as an @mention so GroupLane trigger detection
        # fires even when the user didn't type the mention explicitly.
        # Note: text is already humanized, so inject "@botname" not "<@id>".
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

        node_id = self._get_or_create_cursor(cursor_key)
        author  = UserIdentity(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.name,
        )
        policy = self._build_group_policy()
        member_roles = getattr(message.author, "roles", None)
        msg = InboundMessage(
            tail_node_id=node_id,
            author=author,
            content_type=content_type_for(text, bool(attachments)),
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
            attachments=attachments,
            group_policy=policy,
            server_name=message.guild.name if message.guild else None,
            channel_name=message.channel.name if hasattr(message.channel, "name") else None,
            permission_level=self._resolve_permission_level(member_roles),
        )

        # Check if this message is a trigger BEFORE spawning _handle_turn.
        # Non-trigger messages are just buffered by GroupLane — they will never
        # produce an agent response on their own, so we must not wait on an
        # accumulator for them (that would hold the lane lock and deadlock the
        # next trigger message).
        is_trigger = self._is_group_trigger(text, policy)

        # Proxy-bot compatibility: check compat.json for a matching rule.
        # Only non-webhook messages can be proxied — webhooks are the repost.
        compat_delay: float = self._compat.match(message) if message.webhook_id is None else 0.0

        if not is_trigger:
            # Push to GroupLane for buffering; no accumulator needed.
            if compat_delay > 0:
                async def _delayed_buffer(m=message, msg_=msg) -> None:
                    await asyncio.sleep(compat_delay)
                    try:
                        await m.channel.fetch_message(m.id)
                    except discord.NotFound:
                        logger.debug(
                            "Discord: non-trigger message %s deleted (proxy bot) — dropped",
                            m.id,
                        )
                        return
                    except Exception:
                        pass  # fetch failed for another reason; proceed anyway
                    await self._router.push(msg_)
                task = asyncio.create_task(_delayed_buffer())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                await self._router.push(msg)
            return

        if compat_delay > 0:
            # Delay trigger messages too so the proxy repost arrives first.
            async def _delayed_trigger(
                m=message, msg_=msg, nid=node_id, ch=message.channel,
                ck=cursor_key,
            ) -> None:
                await asyncio.sleep(compat_delay)
                try:
                    await m.channel.fetch_message(m.id)
                except discord.NotFound:
                    logger.debug(
                        "Discord: trigger message %s deleted (proxy bot) — dropped",
                        m.id,
                    )
                    return
                except Exception:
                    pass
                acc_ = _ReplyAccumulator(ch, self._max_len)
                await self._handle_turn(
                    msg_, ch, nid, acc_, ck,
                    record_msg_node=str(m.id),
                )
            task = asyncio.create_task(_delayed_trigger())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        acc  = _ReplyAccumulator(message.channel, self._max_len)
        task = asyncio.create_task(
            self._handle_turn(
                msg, message.channel, node_id, acc, cursor_key,
                record_msg_node=str(message.id),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Thread message handler
    # ------------------------------------------------------------------

    async def _handle_thread_message(self, message: discord.Message) -> None:
        # Defensive self-reply guard — on_message already checks this, but
        # belt-and-suspenders in case call sites change.
        if message.author.bot and (
            self._client.user is None
            or message.author.id == self._client.user.id
        ):
            return

        thread     = message.channel  # discord.Thread
        thread_id  = str(thread.id)
        channel_id = str(thread.parent_id) if thread.parent_id else ""
        cursor_key = f"thread:{thread_id}"

        # In threads, respond to every message (no trigger gating).
        # Threads are already opt-in — you have to come here intentionally.
        text        = message.content.strip()
        attachments = await self._fetch_attachments(message)
        if not text and not attachments:
            return

        node_id = self._get_or_create_thread_cursor(thread_id, channel_id)

        author  = UserIdentity(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            username=message.author.name,
        )
        member_roles = getattr(message.author, "roles", None)
        msg = InboundMessage(
            tail_node_id=node_id,
            author=author,
            content_type=content_type_for(text, bool(attachments)),
            text=text,
            message_id=str(message.id),
            timestamp=time.time(),
            attachments=attachments,
            server_name=message.guild.name if message.guild else None,
            channel_name=thread.name,
            permission_level=self._resolve_permission_level(member_roles),
        )
        acc = _ReplyAccumulator(message.channel, self._max_len)
        task = asyncio.create_task(
            self._handle_turn(msg, message.channel, node_id, acc, cursor_key)
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
        # Wait until the first real activity (thinking/tool/reply chunk) before
        # showing the typing indicator — avoids a spurious indicator on errors
        # that resolve instantly.
        await active_event.wait()
        while not done_event.is_set():
            try:
                # Use the context manager correctly so __aexit__ is always
                # called.  The old code called __aenter__ without __aexit__,
                # leaking discord.py's internal keepalive task on every cycle.
                async with channel.typing():
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=8.0)
                    except asyncio.TimeoutError:
                        pass  # Loop back and send another typing burst
            except Exception:
                await asyncio.sleep(1)

    async def _handle_turn(
        self,
        msg: InboundMessage,
        channel: discord.abc.Messageable,
        node_id: str,
        acc: _ReplyAccumulator,
        cursor_key: str | None = None,
        record_msg_node: str | None = None,
    ) -> None:
        """
        Execute one agent turn.

        Serialised per lane_node_id via _lane_locks so that rapid concurrent
        messages on the same lane don't collide in _accumulators / _typing_active.

        record_msg_node: if set, this is the Discord message ID of the trigger
        message. After the user turn is written to the DB but before the agent
        replies, we snapshot the lane's tail (= the user turn node) and store
        it in the msg->node map so future threads can fork from it precisely.
        """
        # Snapshot the reset epoch before acquiring the lock. If /reset fires
        # during this turn, the epoch will be bumped and we'll skip the final
        # _advance_cursor call so the rewound cursor isn't overwritten.
        epoch_at_start = self._reset_epoch.get(cursor_key, 0) if cursor_key else 0

        lock = self._lane_locks.setdefault(node_id, asyncio.Lock())
        async with lock:
            done_event = asyncio.Event()
            typing_ev  = asyncio.Event()
            self._accumulators[node_id]  = acc
            self._typing_active[node_id] = typing_ev

            try:
                accepted = await self._router.push(msg)
                if not accepted:
                    await channel.send("⏳ I'm busy — please try again in a moment.")
                    return

                # Capture the user-turn node ID immediately after push.
                # At this point the lane has ingested the user message and written
                # its DB node, but hasn't replied yet — so tail == user turn node.
                if record_msg_node:
                    lane = self._router._lane_router._lanes.get(node_id)
                    if lane:
                        user_turn_node_id = lane.loop._tail_node_id
                        if user_turn_node_id:
                            self._store.set_msg_node(record_msg_node, user_turn_node_id)
                            logger.debug(
                                "Discord: mapped message %s -> node %s",
                                record_msg_node, user_turn_node_id,
                            )

                turn_timeout: float | None = float(self._opts.get("turn_timeout_s", 0)) or None
                if self._typing:
                    keepalive = asyncio.create_task(
                        self._typing_keepalive(channel, typing_ev, done_event)
                    )
                    try:
                        await acc.wait_and_send(timeout=turn_timeout)
                    finally:
                        done_event.set()   # stop keepalive loop first
                        typing_ev.set()    # unblock active_event.wait() if never triggered
                        await asyncio.sleep(0)  # let keepalive exit its typing() context cleanly
                        keepalive.cancel()
                        try:
                            await keepalive
                        except asyncio.CancelledError:
                            pass
                else:
                    await acc.wait_and_send(timeout=turn_timeout)

                # Persist the advanced cursor after the full turn completes,
                # but only if no /reset happened while this turn was running.
                if cursor_key:
                    current_epoch = self._reset_epoch.get(cursor_key, 0)
                    if current_epoch == epoch_at_start:
                        self._advance_cursor(cursor_key, node_id)
                    else:
                        logger.debug(
                            "Discord: skipping cursor advance for %s — reset occurred during turn",
                            cursor_key,
                        )

            except Exception:
                logger.exception("Discord: error handling turn for cursor %s", node_id)
            finally:
                done_event.set()
                self._accumulators.pop(node_id, None)
                self._typing_active.pop(node_id, None)

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

        self._router.register_platform_handler(Platform.DISCORD.value, self.handle_event)
        logger.info("Discord bridge: starting (token_env=%s)", token_env)
        await self._client.start(token)


# ---------------------------------------------------------------------------
# Loader entrypoint (called by main.py)
# ---------------------------------------------------------------------------

async def run(router: "Router") -> None:
    """Entry point called by main.py bridge loader."""
    bridge_cfg = router.config.bridges.get("discord")
    options: dict = bridge_cfg.options if bridge_cfg else {}
    bridge = DiscordBridge(router, options)
    await bridge.run()

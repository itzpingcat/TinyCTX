"""
modules/sysops/__main__.py

System operation tools: user/permission management, plus the /model
command and its set_active_model tool equivalent for switching the LLM
used on a conversation branch.

Tools registered (all always_on=False):
  user_list           — list all users                       min_permission=50
  user_info           — show one user's details               min_permission=50
  user_modify_permissions — set a user's permission_level     min_permission=50
  user_rename         — rename a TinyCTX username             min_permission=100
  user_merge          — merge two users into one              min_permission=100
  set_active_model    — override/clear the LLM for this branch  min_permission=75
                        (see __init__.py's EXTENSION_META.default_config.model_min_permission)

Slash commands registered (via runtime.commands):
  /model              — show the current effective model
  /model list         — list configured chat models
  /model clear        — clear the override
  /model <name>       — set the override
  (same min_permission as set_active_model, enforced independently since
  slash-command dispatch happens outside an AgentCycle — see
  _resolve_model_caller below)

Permission rules enforced at call time (not just at registration):
  - user_modify_permissions: caller can only promote to at most (their level - 1).
  - user_modify_permissions: caller can only demote users whose current level is at most (their level - 1).
  - user_rename / user_merge: caller must be level 100.
  - set_active_model / /model: caller must be >= model_min_permission.

The runtime's UserStore is captured once in register_runtime and shared
across all cycles via a module-level reference. Tool closures read
agent.permission_level at call time so the check reflects the actual
caller, not a stale snapshot.

How the model override takes effect
-------------------------------------
set_active_model / /model only WRITE state. AgentCycle.run() (agent.py)
already reads it on every cycle:

    state, _ = self.db.load_session_state(node_id)
    primary_name = state.get("model") or self.config.llm.primary

so as soon as the "model" key is written into the state_delta chain for a
branch, it becomes the primary model for every subsequent turn on that
branch, until cleared or overridden again. Writes go through
db.set_state() (merge-write), not db.update_node_state_delta() (blind
full-column replace) — see db.py's set_state()/get_state() docstrings and
CODEBASE.md's Database section for why the raw primitive is a footgun for
multi-writer nodes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level references set by register_runtime.
# All tool/command closures read from these.
_users = None
_runtime = None


def register_runtime(runtime) -> None:
    global _users, _runtime
    _users = runtime.users
    _runtime = runtime
    logger.info("[sysops] registered — UserStore at %s", id(_users))

    _register_model_command(runtime)


# ===========================================================================
# /model slash command
# ===========================================================================
#
# Slash-command dispatch happens outside an AgentCycle, so there's no
# agent.caller the way tools get one. The caller's identity is instead
# resolved from the conversation branch itself: the node_id/cursor the
# bridge puts in `context` already has platform + author_id somewhere in
# its session state (written by Runtime._compute_state_delta on the inbound
# user node), so we load_session_state() on it and resolve the User via
# runtime.users.get_by_platform — the same approach
# modules/equipment_manifest/__main__.py uses for its own trust check.

async def _model_reply(context: dict, text: str) -> None:
    """Works whether the bridge gives an async 'send' callable (Discord) or
    a sync 'console' with .print() (gateway's _StringConsole)."""
    send = context.get("send")
    if callable(send):
        await send(text)
        return
    console = context.get("console")
    if console is not None:
        console.print(text)


def _resolve_model_node_id(context: dict) -> str:
    """Bridges disagree on the key name — gateway uses 'node_id', Discord uses 'cursor'."""
    return (context.get("node_id") or context.get("cursor") or "").strip()


def _resolve_model_caller(runtime, node_id: str):
    if not node_id:
        return None
    state, _ = runtime.db.load_session_state(node_id)
    # NOTE: session state's "author_id" is the TinyCTX username (see
    # runtime.py's _compute_state_delta — mapping["author_id"] =
    # msg.author.username), not the platform-native user_id. Look it up
    # via get_user(), not get_by_platform() (which expects a platform user_id
    # and would never match a username).
    author_id = state.get("author_id")
    if not author_id:
        return None
    try:
        return runtime.users.get_user(author_id)
    except Exception:
        logger.debug("[sysops] failed to resolve /model caller for node %s", node_id, exc_info=True)
        return None


def _chat_model_names(config) -> list[str]:
    """Names of configured models usable as a primary/fallback LLM (excludes embedding models)."""
    return sorted(name for name, mc in config.models.items() if not mc.is_embedding)


def _model_status_text(db, config, node_id: str) -> str:
    override = db.get_state(node_id, "model", "") or ""
    default = config.llm.primary
    if override:
        return f"Current model: {override} (override — default is {default})"
    return f"Current model: {default} (default, no override set)"


def _model_list_text(db, config, node_id: str) -> str:
    override = db.get_state(node_id, "model", "") or ""
    default = config.llm.primary
    names = _chat_model_names(config)
    if not names:
        return "No chat models configured."
    lines = ["Available models:"]
    for name in names:
        tags = []
        if name == default:
            tags.append("default")
        if name == override:
            tags.append("current override")
        suffix = f"  ({', '.join(tags)})" if tags else ""
        lines.append(f"  {name}{suffix}")
    return "\n".join(lines)


def _model_min_permission(runtime) -> int:
    try:
        from TinyCTX.modules.sysops import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}
    if hasattr(runtime.config, "extra") and isinstance(runtime.config.extra, dict):
        cfg = {**cfg, **runtime.config.extra.get("sysops", {})}
    return int(cfg.get("model_min_permission", 75))


def _register_model_command(runtime) -> None:
    min_permission = _model_min_permission(runtime)

    async def _cmd_model(args: list[str], context: dict) -> None:
        node_id = _resolve_model_node_id(context)
        caller = _resolve_model_caller(runtime, node_id)
        if caller is None:
            await _model_reply(context, "⛔ Cannot resolve your identity for this conversation.")
            return
        if caller.permission_level < min_permission:
            await _model_reply(
                context,
                f"⛔ /model requires permission level {min_permission} "
                f"(yours is {caller.permission_level}).",
            )
            return
        if not node_id:
            await _model_reply(context, "⛔ No conversation to attach the override to.")
            return

        if not args:
            await _model_reply(context, _model_status_text(runtime.db, runtime.config, node_id))
            return

        sub = args[0].lower()

        if sub == "list":
            await _model_reply(context, _model_list_text(runtime.db, runtime.config, node_id))
            return

        if sub == "clear":
            runtime.db.set_state(node_id, "model", "")
            await _model_reply(context, f"Model override cleared — back to default ({runtime.config.llm.primary}).")
            return

        name = args[0]
        valid = _chat_model_names(runtime.config)
        if name not in valid:
            await _model_reply(
                context,
                f"⛔ Unknown model '{name}'. Available: {', '.join(valid) or '(none configured)'}",
            )
            return

        runtime.db.set_state(node_id, "model", name)
        await _model_reply(context, f"Model override set: {name}")

    runtime.commands.register(
        "model", "", _cmd_model,
        help=f"Show/set/clear the LLM model for this conversation (permission {min_permission}+)",
        params=[("model_name", str, "Model name, or 'list' / 'clear' — leave blank to show current")],
    )
    logger.info("[sysops] /model registered (min_permission=%d)", min_permission)


def register_agent(agent) -> None:
    if _users is None:
        logger.warning("[sysops] UserStore not available — skipping tool registration")
        return

    users = _users
    # Snapshot caller level once per cycle. The closure captures the *variable*
    # not the value, but agent.permission_level is set before register_agent is
    # called and never changes within a cycle, so this is safe.
    caller_level = agent.caller.permission_level

    # ------------------------------------------------------------------
    # user_list
    # ------------------------------------------------------------------

    def user_list(platform: str = "") -> str:
        """List all TinyCTX users.

        Args:
            platform: Optional platform name to filter by (e.g. 'discord', 'cli').
                      Leave blank to show all users.
        """
        rows = users._conn.execute(
            "SELECT username, permission_level, identities, created_at "
            "FROM users ORDER BY permission_level DESC, username ASC"
        ).fetchall()

        if not rows:
            return "No users found."

        import json as _json
        lines = []
        for row in rows:
            identities = _json.loads(row["identities"])
            id_strs = [
                f"{i['platform']}:{i['user_id']} ({i['username']})"
                for i in identities
                if not platform or i["platform"] == platform
            ]
            if platform and not id_strs:
                continue
            lines.append(
                f"{row['username']}  level={row['permission_level']}  "
                + (", ".join(id_strs) if id_strs else "no identities")
            )

        if not lines:
            return f"No users with platform '{platform}'."
        return f"{len(lines)} user(s):\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # user_info
    # ------------------------------------------------------------------

    def user_info(username: str) -> str:
        """Show full details for a single TinyCTX user.

        Args:
            username: TinyCTX username to look up.
        """
        user = users.get_user(username)
        if user is None:
            return f"User '{username}' not found."

        import json as _json, time as _time
        identities = "\n".join(
            f"  {i.platform.value}:{i.user_id}  username={i.username}  display={i.display_name}"
            for i in user.identities
        ) or "  (none)"
        created = _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime(user.created_at))
        meta = _json.dumps(user.meta, indent=2) if user.meta else "{}"
        return (
            f"username:    {user.username}\n"
            f"level:       {user.permission_level}\n"
            f"created:     {created}\n"
            f"identities:\n{identities}\n"
            f"meta: {meta}"
        )

    # ------------------------------------------------------------------
    # user_modify_permissions
    # ------------------------------------------------------------------

    def user_modify_permissions(username: str, level: int) -> str:
        """Set a user's permission_level.

        Permission rules:
          - You can only promote a user to at most (your level - 1).
          - You can only demote users whose current level is at most (your level - 1).
          - Level must be between 0 and 100.

        Args:
            username: TinyCTX username to modify.
            level:    New permission level (0-100).
        """
        try:
            level = int(level)
        except (ValueError, TypeError):
            return f"Error: level must be an integer, got {level!r}."
        if not (0 <= level <= 100):
            return f"Error: level must be 0-100, got {level}."
        max_grantable = caller_level - 1
        if level > max_grantable:
            return (
                f"Error: cannot set level {level} — "
                f"you may only grant up to level {max_grantable} (your level - 1)."
            )

        user = users.get_user(username)
        if user is None:
            return f"User '{username}' not found."

        if user.permission_level >= caller_level:
            return (
                f"Error: '{username}' has level {user.permission_level}, "
                f"which is not below your level ({caller_level}). "
                "You can only modify users at least 1 level below you."
            )

        old = user.permission_level
        user.permission_level = level
        users.update_user(user)
        logger.info(
            "[sysops] user_modify_permissions: '%s' level %d → %d (caller_level=%d)",
            username, old, level, caller_level,
        )
        return f"'{username}': permission_level {old} → {level}."

    # ------------------------------------------------------------------
    # user_rename
    # ------------------------------------------------------------------

    def user_rename(username: str, new_username: str) -> str:
        """Rename a TinyCTX username. Requires caller level 100.

        Updates both the users table and the platform index atomically.
        The user's identities, level, and meta are unchanged.

        Args:
            username:     Current TinyCTX username.
            new_username: New TinyCTX username (must not already be taken).
        """
        if caller_level < 100:
            return f"Error: user_rename requires level 100 (yours is {caller_level})."

        from TinyCTX.users import UsernameConflictError
        try:
            updated = users.rename_user(username, new_username)
            logger.info(
                "[sysops] user_rename: '%s' → '%s' (caller_level=%d)",
                username, updated.username, caller_level,
            )
            return f"Renamed '{username}' → '{updated.username}'."
        except ValueError as exc:
            return f"Error: {exc}"
        except UsernameConflictError:
            return f"Error: username '{new_username}' is already taken."

    # ------------------------------------------------------------------
    # user_merge
    # ------------------------------------------------------------------

    def user_merge(primary_username: str, secondary_username: str) -> str:
        """Merge two users: move all platform identities from secondary into primary,
        then delete the secondary user. Requires caller level 100.

        Use this when the same human has two separate TinyCTX user records
        (e.g. created separately on Discord and Matrix before being linked).
        After merging, all of secondary's identities are accessible via primary.

        Args:
            primary_username:   The user to keep. Receives all identities.
            secondary_username: The user to delete after merging.
        """
        if caller_level < 100:
            return f"Error: user_merge requires level 100 (yours is {caller_level})."

        try:
            merged = users.merge_users(primary_username, secondary_username)
            id_count = len(merged.identities)
            logger.info(
                "[sysops] user_merge: '%s' absorbed '%s', now %d identities (caller_level=%d)",
                primary_username, secondary_username, id_count, caller_level,
            )
            return (
                f"Merged '{secondary_username}' into '{primary_username}'. "
                f"'{primary_username}' now has {id_count} platform identity(s)."
            )
        except ValueError as exc:
            return f"Error: {exc}"

    # ------------------------------------------------------------------
    # set_active_model — agent-callable equivalent of /model
    # ------------------------------------------------------------------

    model_min_permission = _model_min_permission(_runtime) if _runtime is not None else 75

    def set_active_model(name: str) -> str:
        """Set (or clear) the LLM model override for this conversation branch.

        Same effect as the /model slash command: writes to session state,
        which agent.py reads on every subsequent cycle on this branch
        (state.get("model") or config default). Must be a chat model
        defined under models: in config.yaml — embedding models are
        rejected. Pass "" or "default" to clear the override and revert to
        the configured default (config.llm.primary).

        Args:
            name: Model name from config.yaml's models: block, or "" / "default" to clear.
        """
        if caller_level < model_min_permission:
            return f"Error: set_active_model requires level {model_min_permission} (yours is {caller_level})."

        default = agent.config.llm.primary
        if name in ("", "default"):
            agent.db.set_state(agent.context.tail_node_id, "model", "")
            logger.info("[sysops] set_active_model: cleared (caller_level=%d)", caller_level)
            return f"Model override cleared — back to default ({default})."

        valid = _chat_model_names(agent.config)
        if name not in valid:
            return f"Error: unknown model '{name}'. Available: {', '.join(valid) or '(none configured)'}"

        agent.db.set_state(agent.context.tail_node_id, "model", name)
        logger.info("[sysops] set_active_model: '%s' (caller_level=%d)", name, caller_level)
        return f"Model override set: {name}"

    # ------------------------------------------------------------------
    # Register
    # ------------------------------------------------------------------

    agent.tool_handler.register_tool(user_list,   always_on=False, min_permission=50)
    agent.tool_handler.register_tool(user_info,   always_on=False, min_permission=50)
    agent.tool_handler.register_tool(user_modify_permissions, always_on=False, min_permission=50)
    agent.tool_handler.register_tool(user_rename, always_on=False, min_permission=100)
    agent.tool_handler.register_tool(user_merge,  always_on=False, min_permission=100)
    agent.tool_handler.register_tool(set_active_model, always_on=False, min_permission=model_min_permission)

    logger.debug(
        "[sysops] registered 6 tools for caller=%s level=%d",
        agent.caller.username, caller_level,
    )


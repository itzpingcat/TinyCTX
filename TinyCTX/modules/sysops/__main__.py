"""
modules/sysops/__main__.py

System operation tools: user/permission management, plus the /model
command and its set_active_model tool equivalent for switching the LLM
used on a conversation branch.

Tools registered (all always_on=False), gated per docs/PERMISSIONS-PLAN.md
§10.1 at the ToolCallHandler seam (TinyCTX.permissions.Permission):
  user_list                — list all users                    USER_READ
  user_info                — show one user's details            USER_READ
  user_modify_permissions  — set a user's permission template    ROOT
  user_rename              — rename a TinyCTX username           ROOT
  user_merge                — merge two users into one            ROOT
  set_active_model          — override/clear the LLM for this branch  MODEL_SWAP

Slash commands registered (via runtime.commands), gated at the
CommandRegistry seam with the same bool:
  /model              — show the current effective model
  /model list         — list configured chat models
  /model clear        — clear the override
  /model <name>       — set the override
  (MODEL_SWAP, same named bool as set_active_model — dispatch() checks it
  centrally before _cmd_model ever runs; see docs/PERMISSIONS-PLAN.md §9)

There is no more numeric ceiling logic ("can only promote to at most your
own level - 1"): ROOT is total (see permissions.py's docstring), and every
tool/command above is gated by a bool the caller either holds or doesn't —
by the time each function body below runs, the seam has already confirmed
the caller holds what's required.

The runtime's UserStore is captured once in register_runtime and shared
across all cycles via a module-level reference.

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

from TinyCTX.permissions import Permission

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
        lines.append(f"- {name}{suffix}")
    return "\n".join(lines)


def _register_model_command(runtime) -> None:
    async def _cmd_model(args: list[str], context: dict) -> None:
        # Permission (MODEL_SWAP) was already checked by
        # CommandRegistry.dispatch() before this handler ever ran — see the
        # required_permissions= passed to register() below. This handler
        # only needs the node_id to attach state to, no caller resolution.
        node_id = _resolve_model_node_id(context)
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
        help="Show/set/clear the LLM model for this conversation (requires model_swap)",
        params=[("model_name", str, "Model name, or 'list' / 'clear' — leave blank to show current")],
        required_permissions={Permission.MODEL_SWAP},
    )
    logger.info("[sysops] /model registered (required: model_swap)")


def register_agent(agent) -> None:
    if _users is None:
        logger.warning("[sysops] UserStore not available — skipping tool registration")
        return

    users = _users
    permissions_config = agent.config.permissions

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
            "SELECT username, permission_template, identities, created_at "
            "FROM users ORDER BY username ASC"
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
            template = row["permission_template"] or f"{permissions_config.default_template} (default)"
            lines.append(
                f"{row['username']}  template={template}  "
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
        template = user.permission_template or f"{permissions_config.default_template} (default)"
        effective = sorted(p.value for p in user.effective_permissions(permissions_config))
        return (
            f"username:    {user.username}\n"
            f"template:    {template}\n"
            f"effective:   {', '.join(effective) or '(none)'}\n"
            f"created:     {created}\n"
            f"identities:\n{identities}\n"
            f"meta: {meta}"
        )

    # ------------------------------------------------------------------
    # user_modify_permissions
    # ------------------------------------------------------------------

    def user_modify_permissions(username: str, template: str) -> str:
        """Set a user's named permission template.

        Templates are configured centrally under permissions.templates in
        config.yaml (see TinyCTX.config.PermissionsConfig). There is no
        ceiling check — ROOT is total, so a ROOT holder may set any user,
        including themselves, to any known template.

        Args:
            username: TinyCTX username to modify.
            template: Name of a template configured under permissions.templates.
        """
        if template not in permissions_config.templates:
            return (
                f"Error: unknown template {template!r}. "
                f"Known templates: {sorted(permissions_config.templates)}"
            )

        user = users.get_user(username)
        if user is None:
            return f"User '{username}' not found."

        old_template = user.permission_template or permissions_config.default_template
        user.permission_template = template
        users.update_user(user)
        logger.info(
            "[sysops] user_modify_permissions: '%s' template %r → %r (caller=%s)",
            username, old_template, template, agent.caller.username,
        )
        return f"'{username}': {old_template} → {template}."

    # ------------------------------------------------------------------
    # user_rename
    # ------------------------------------------------------------------

    def user_rename(username: str, new_username: str) -> str:
        """Rename a TinyCTX username. Requires the root capability.

        Updates both the users table and the platform index atomically.
        The user's identities, permission template, and meta are unchanged.

        Args:
            username:     Current TinyCTX username.
            new_username: New TinyCTX username (must not already be taken).
        """
        from TinyCTX.users import UsernameConflictError
        try:
            updated = users.rename_user(username, new_username)
            logger.info(
                "[sysops] user_rename: '%s' → '%s' (caller=%s)",
                username, updated.username, agent.caller.username,
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
        then delete the secondary user. Requires the root capability.

        Use this when the same human has two separate TinyCTX user records
        (e.g. created separately on Discord and Matrix before being linked).
        After merging, all of secondary's identities are accessible via primary.

        Args:
            primary_username:   The user to keep. Receives all identities.
            secondary_username: The user to delete after merging.
        """
        try:
            merged = users.merge_users(primary_username, secondary_username)
            id_count = len(merged.identities)
            logger.info(
                "[sysops] user_merge: '%s' absorbed '%s', now %d identities (caller=%s)",
                primary_username, secondary_username, id_count, agent.caller.username,
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
        default = agent.config.llm.primary
        if name in ("", "default"):
            agent.db.set_state(agent.context.tail_node_id, "model", "")
            logger.info("[sysops] set_active_model: cleared (caller=%s)", agent.caller.username)
            return f"Model override cleared — back to default ({default})."

        valid = _chat_model_names(agent.config)
        if name not in valid:
            return f"Error: unknown model '{name}'. Available: {', '.join(valid) or '(none configured)'}"

        agent.db.set_state(agent.context.tail_node_id, "model", name)
        logger.info("[sysops] set_active_model: '%s' (caller=%s)", name, agent.caller.username)
        return f"Model override set: {name}"

    # ------------------------------------------------------------------
    # Register
    # ------------------------------------------------------------------

    agent.tool_handler.register_tool(user_list,   always_on=False, required_permissions={Permission.USER_READ})
    agent.tool_handler.register_tool(user_info,   always_on=False, required_permissions={Permission.USER_READ})
    agent.tool_handler.register_tool(user_modify_permissions, always_on=False, required_permissions={Permission.ROOT})
    agent.tool_handler.register_tool(user_rename, always_on=False, required_permissions={Permission.ROOT})
    agent.tool_handler.register_tool(user_merge,  always_on=False, required_permissions={Permission.ROOT})
    agent.tool_handler.register_tool(set_active_model, always_on=False, required_permissions={Permission.MODEL_SWAP})

    logger.debug(
        "[sysops] registered 6 tools for caller=%s",
        agent.caller.username,
    )


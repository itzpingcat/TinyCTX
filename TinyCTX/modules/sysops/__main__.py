"""
modules/sysops/__main__.py

Agent-callable tools for user and permission management.

Tools registered (all always_on=False):
  user_list   — list all users                     min_permission=50
  user_info   — show one user's details            min_permission=50
  user_modify_permissions — set a user's permission_level  min_permission=50
  user_rename — rename a TinyCTX username          min_permission=100
  user_merge  — merge two users into one           min_permission=100

Permission rules enforced at call time (not just at registration):
  - user_modify_permissions: caller can only promote to at most (their level - 1).
  - user_modify_permissions: caller can only demote users whose current level is at most (their level - 1).
  - user_rename / user_merge: caller must be level 100.

The runtime's UserStore is captured once in register_runtime and shared
across all cycles via a module-level reference. Tool closures read
agent.permission_level at call time so the check reflects the actual
caller, not a stale snapshot.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level reference set by register_runtime.
# All tool closures read from this.
_users = None


def register_runtime(runtime) -> None:
    global _users
    _users = runtime.users
    logger.info("[sysops] registered — UserStore at %s", id(_users))


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
    # Register
    # ------------------------------------------------------------------

    agent.tool_handler.register_tool(user_list,   always_on=False, min_permission=50)
    agent.tool_handler.register_tool(user_info,   always_on=False, min_permission=50)
    agent.tool_handler.register_tool(user_modify_permissions, always_on=False, min_permission=50)
    agent.tool_handler.register_tool(user_rename, always_on=False, min_permission=100)
    agent.tool_handler.register_tool(user_merge,  always_on=False, min_permission=100)

    logger.debug(
        "[sysops] registered 5 tools for caller=%s level=%d",
        agent.caller.username, caller_level,
    )


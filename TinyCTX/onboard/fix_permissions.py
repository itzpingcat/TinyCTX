"""
onboard/fix_permissions.py — Permission elevation utility for TinyCTX.

Callable standalone (bypasses normal "can't grant above your own level" check —
physical access to the machine running TinyCTX is the authorization):

    python -m TinyCTX.onboard.fix_permissions --user USERNAME
    python -m TinyCTX.onboard.fix_permissions --user USERNAME --level 50

Or imported and called from other code:

    from TinyCTX.onboard.fix_permissions import elevate_user, list_users
"""

from __future__ import annotations

import argparse
import sys

from TinyCTX.users import UserStore
from TinyCTX.users.models import User


def elevate_user(username: str, level: int = 100, store: UserStore | None = None) -> User:
    """
    Set permission_level for a TinyCTX username.

    No caller-level check — this is the privileged path used by the CLI admin
    console and the standalone script.  Authorization is physical access to
    the machine (you already have the gateway api_key and shell access).

    Args:
        username: TinyCTX username to modify.
        level:    Permission level to assign (0-100). Default 100.
        store:    Existing UserStore. If None, a fresh one is opened.

    Returns the updated User.

    Raises:
        ValueError       if username not found or level out of range.
    """
    if not (0 <= level <= 100):
        raise ValueError(f"level must be 0-100, got {level}")

    if store is None:
        store = UserStore()

    user = store.get_user(username)
    if user is None:
        raise ValueError(f"User {username!r} not found in users.db")

    user.permission_level = level
    store.update_user(user)
    return user


def list_users(store: UserStore | None = None) -> list[User]:
    """Return all users sorted by permission_level descending, then username."""
    if store is None:
        store = UserStore()
    rows = store._conn.execute(
        "SELECT username FROM users ORDER BY permission_level DESC, username ASC"
    ).fetchall()
    users = []
    for row in rows:
        u = store.get_user(row["username"])
        if u:
            users.append(u)
    return users


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m TinyCTX.onboard.fix_permissions",
        description=(
            "Directly set a TinyCTX user's permission level.\n"
            "No caller-level check — requires shell access to the TinyCTX host."
        ),
    )
    parser.add_argument(
        "--user",
        metavar="USERNAME",
        required=True,
        help="TinyCTX username to modify.",
    )
    parser.add_argument(
        "--level",
        type=int,
        default=100,
        metavar="LEVEL",
        help="Permission level to assign (0-100). Default: 100.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all users and their current permission levels.",
    )
    args = parser.parse_args()

    store = UserStore()

    if args.list:
        users = list_users(store)
        if not users:
            print("No users found.")
        else:
            print(f"{'USERNAME':<32}  {'LEVEL':>5}  IDENTITIES")
            print("-" * 72)
            for u in users:
                identities = ", ".join(
                    f"{i.platform.value}:{i.user_id}" for i in u.identities
                ) or "—"
                print(f"{u.username:<32}  {u.permission_level:>5}  {identities}")
        return

    try:
        user = elevate_user(args.user, args.level, store)
        print(f"User '{user.username}' permission_level set to {user.permission_level}.")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

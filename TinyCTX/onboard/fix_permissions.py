"""
onboard/fix_permissions.py — Permission elevation utility for TinyCTX.

Callable standalone (bypasses normal permission checks entirely — this tool
has no ROOT-holder ceiling logic to bypass in the first place now that ROOT
is total, but the point stands: physical access to the machine running
TinyCTX is the authorization):

    python -m TinyCTX.onboard.fix_permissions --user USERNAME
    python -m TinyCTX.onboard.fix_permissions --user USERNAME --reset

Or imported and called from other code:

    from TinyCTX.onboard.fix_permissions import elevate_user, reset_user, list_users
"""

from __future__ import annotations

import argparse
import sys

from TinyCTX.permissions import Permission
from TinyCTX.users import UserStore
from TinyCTX.users.models import User


def elevate_user(username: str, store: UserStore | None = None) -> User:
    """
    Grant a TinyCTX username every permission bool via a full
    permission_overrides dict — the single-global-template equivalent of
    the old "operator" tier. There is one permissions.template (config.yaml)
    now, shared by every user, so "elevate" no longer means "reassign to a
    different named tier"; it means "override every bool to true for this
    one user".

    No caller-permission check — this is the privileged path used by the CLI
    admin console and the standalone script. Authorization is physical
    access to the machine (you already have the gateway api_key and shell
    access).

    Args:
        username: TinyCTX username to modify.
        store:    Existing UserStore. If None, a fresh one is opened.

    Returns the updated User.

    Raises:
        ValueError if username not found.
    """
    if store is None:
        store = UserStore()

    user = store.get_user(username)
    if user is None:
        raise ValueError(f"User {username!r} not found in users.db")

    user.permission_overrides = {p.value: True for p in Permission}
    store.update_user(user)
    return user


def reset_user(username: str, store: UserStore | None = None) -> User:
    """
    Clear a TinyCTX username's permission_overrides, returning them to
    whatever the single global permissions.template (config.yaml) grants
    everyone by default. The inverse of elevate_user().
    """
    if store is None:
        store = UserStore()

    user = store.get_user(username)
    if user is None:
        raise ValueError(f"User {username!r} not found in users.db")

    user.permission_overrides = {}
    store.update_user(user)
    return user


def is_elevated(user: User) -> bool:
    """True if this user's overrides grant every Permission bool — i.e.
    elevate_user() has been run on them and nothing since revoked a bool."""
    return all(user.permission_overrides.get(p.value) is True for p in Permission)


def list_users(store: UserStore | None = None) -> list[User]:
    """Return all users sorted by username."""
    if store is None:
        store = UserStore()
    rows = store._conn.execute(
        "SELECT username FROM users ORDER BY username ASC"
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
            "Grant or revoke full admin access for a TinyCTX user.\n"
            "No caller-permission check — requires shell access to the TinyCTX host."
        ),
    )
    parser.add_argument(
        "--user",
        metavar="USERNAME",
        required=True,
        help="TinyCTX username to modify.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear this user's overrides instead of granting every permission "
             "(returns them to whatever permissions.template grants everyone).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all users and whether they're currently elevated.",
    )
    args = parser.parse_args()

    store = UserStore()

    if args.list:
        users = list_users(store)
        if not users:
            print("No users found.")
        else:
            print(f"{'USERNAME':<32}  {'ADMIN':<8}  IDENTITIES")
            print("-" * 80)
            for u in users:
                identities = ", ".join(
                    f"{i.platform.value}:{i.user_id}" for i in u.identities
                ) or "—"
                print(f"{u.username:<32}  {('yes' if is_elevated(u) else 'no'):<8}  {identities}")
        return

    try:
        if args.reset:
            user = reset_user(args.user, store)
            print(f"User '{user.username}' overrides cleared (back to permissions.template).")
        else:
            user = elevate_user(args.user, store)
            print(f"User '{user.username}' elevated — every permission granted.")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

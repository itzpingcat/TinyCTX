"""
onboard/fix_permissions.py — Permission elevation utility for TinyCTX.

Callable standalone (bypasses normal permission checks entirely — this tool
has no ROOT-holder ceiling logic to bypass in the first place now that ROOT
is total, but the point stands: physical access to the machine running
TinyCTX is the authorization):

    python -m TinyCTX.onboard.fix_permissions --user USERNAME
    python -m TinyCTX.onboard.fix_permissions --user USERNAME --template operator

Or imported and called from other code:

    from TinyCTX.onboard.fix_permissions import elevate_user, list_users
"""

from __future__ import annotations

import argparse
import sys

from TinyCTX.users import UserStore
from TinyCTX.users.models import User

# The template this module's CLI/step_bootstrap_admin flow assigns by
# default — the operator template is expected to include ROOT (see
# config/__main__.py's _BUILTIN_TEMPLATES and docs/PERMISSIONS-PLAN.md §2).
DEFAULT_ELEVATE_TEMPLATE = "operator"


def elevate_user(username: str, template: str = DEFAULT_ELEVATE_TEMPLATE, store: UserStore | None = None) -> User:
    """
    Set a TinyCTX username's permission_template.

    No caller-permission check — this is the privileged path used by the CLI
    admin console and the standalone script. Authorization is physical
    access to the machine (you already have the gateway api_key and shell
    access). Does not validate `template` against permissions.templates in
    config.yaml — this tool deliberately doesn't load the full Config, so an
    unknown template name is accepted as-is (User.effective_permissions()
    falls back to the empty set for an unknown template at read time, same
    as any other unknown-template user).

    Args:
        username: TinyCTX username to modify.
        template: Name of the permission template to assign. Default "operator".
        store:    Existing UserStore. If None, a fresh one is opened.

    Returns the updated User.

    Raises:
        ValueError if username not found.
    """
    if not template or not template.strip():
        raise ValueError("template must be a non-empty string")

    if store is None:
        store = UserStore()

    user = store.get_user(username)
    if user is None:
        raise ValueError(f"User {username!r} not found in users.db")

    user.permission_template = template.strip()
    store.update_user(user)
    return user


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
            "Directly set a TinyCTX user's permission template.\n"
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
        "--template",
        type=str,
        default=DEFAULT_ELEVATE_TEMPLATE,
        metavar="TEMPLATE",
        help=f"Permission template to assign. Default: {DEFAULT_ELEVATE_TEMPLATE!r}.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all users and their current permission templates.",
    )
    args = parser.parse_args()

    store = UserStore()

    if args.list:
        users = list_users(store)
        if not users:
            print("No users found.")
        else:
            print(f"{'USERNAME':<32}  {'TEMPLATE':<16}  IDENTITIES")
            print("-" * 80)
            for u in users:
                identities = ", ".join(
                    f"{i.platform.value}:{i.user_id}" for i in u.identities
                ) or "—"
                print(f"{u.username:<32}  {(u.permission_template or '(default)'):<16}  {identities}")
        return

    try:
        user = elevate_user(args.user, args.template, store)
        print(f"User '{user.username}' permission_template set to {user.permission_template!r}.")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
scripts/repair_users_db.py — One-off repair for a users.db written by the
old (buggy) permissions migration.

TinyCTX/users/store.py's _migrate() used to freeze a FULL explicit
permission_overrides dict (every Permission name present) for every user
migrated off permission_level/permission_template, instead of a sparse
diff against permissions.template. That's fixed at the source now (see
_sparse_overrides_for in users/store.py), but a users.db that already went
through the old migration is past the guard — it only has the current
permission_overrides column, so _migrate() sees nothing to do and won't
retroactively shrink it. This script does that shrink directly.

Idempotent: a row that's already sparse re-diffs to the same sparse dict,
so re-running this changes nothing.

Instance directory resolved via utils/instance.py, same as every other
TinyCTX CLI command (status/start/stop/onboard): --dir, else .tinyctx/ in
the current directory, else ~/.tinyctx. --config overrides config.yaml's
location directly. Loaded through TinyCTX.config.load(), the same loader
every other entrypoint uses — not a hand-rolled YAML read — so the
resolved permissions.template is exactly what the running instance sees.

Usage:
    python -m scripts.repair_users_db
    python -m scripts.repair_users_db --dir /path/to/.tinyctx
    python -m scripts.repair_users_db --config /path/to/config.yaml
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from TinyCTX.config import load as load_config
from TinyCTX.permissions import Permission
from TinyCTX.utils.instance import config_path_for, resolve_instance_dir


def sparse_overrides_for(resolved: frozenset, template: frozenset) -> dict:
    """Same diff users/store.py's _sparse_overrides_for does: keep only the
    entries where resolved and template disagree."""
    return {p.value: (p in resolved) for p in Permission if (p in resolved) != (p in template)}


def repair(db_path: Path, template: frozenset) -> int:
    """Shrink every user's permission_overrides row to a sparse diff against
    `template`, preserving each user's actual effective permissions.
    Returns the number of rows changed."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT username, permission_overrides FROM users").fetchall()
    changed = 0
    for row in rows:
        try:
            raw_overrides = json.loads(row["permission_overrides"]) if row["permission_overrides"] else {}
        except json.JSONDecodeError:
            print(f"  skip {row['username']!r}: corrupt JSON, leaving alone")
            continue
        if not isinstance(raw_overrides, dict):
            print(f"  skip {row['username']!r}: not an object, leaving alone")
            continue

        # Resolve what this user's stored overrides currently grant, the
        # same unknown-key-dropping tolerance as User.effective_permissions.
        resolved_overrides: dict[Permission, bool] = {}
        for name, value in raw_overrides.items():
            try:
                perm = Permission(name)
            except ValueError:
                continue
            resolved_overrides[perm] = bool(value)

        granted = set(template)
        for perm, value in resolved_overrides.items():
            if value:
                granted.add(perm)
            else:
                granted.discard(perm)

        sparse = sparse_overrides_for(frozenset(granted), template)

        if sparse != raw_overrides:
            conn.execute(
                "UPDATE users SET permission_overrides = ? WHERE username = ?",
                (json.dumps(sparse), row["username"]),
            )
            changed += 1
            print(f"  {row['username']!r}: {len(raw_overrides)} keys -> {len(sparse)} keys")

    conn.commit()
    conn.close()
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.repair_users_db",
        description=(
            "Shrink users.db's permission_overrides rows from a full "
            "explicit dict (the old migration bug) down to a sparse diff "
            "against permissions.template."
        ),
    )
    parser.add_argument("--dir", metavar="PATH", help="Path to a .tinyctx instance directory.")
    parser.add_argument("--config", metavar="PATH", help="Path to config.yaml directly (overrides --dir/autodetect).")
    args = parser.parse_args()

    instance_dir = resolve_instance_dir(args.dir)
    config_path = Path(args.config or config_path_for(instance_dir)).resolve()
    if not config_path.exists():
        print(f"error: no config.yaml found at {config_path}.", file=sys.stderr)
        print("  Pass --dir or --config, or run 'tinyctx onboard' first.", file=sys.stderr)
        sys.exit(1)

    db_path = instance_dir / "data" / "users.db"
    if not db_path.exists():
        print(f"error: no users.db found at {db_path}.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(str(config_path))
    template = cfg.permissions.template
    print(f"Instance: {instance_dir}")
    print(f"users.db: {db_path}")
    print(f"permissions.template = {sorted(p.value for p in template)}")

    changed = repair(db_path, template)
    total = sqlite3.connect(str(db_path)).execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"Done. {changed}/{total} rows shrunk.")


if __name__ == "__main__":
    main()

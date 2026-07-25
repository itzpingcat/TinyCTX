#!/usr/bin/env python3
"""
unscope_to_global.py — Reset every entity's scope back to "global".

Failure mode this fixes: the extractor librarian defaulted new/updated
entities to a narrow `user:<name>` scope far more often than the "narrow
scope ONLY for sensitive/personal info" rule in extractor_system.txt
intends. Once most entities live under `user:*` scopes, other agents/users
resolve a *different* visible-scope set (modules/memory/scopes.py
resolve_scopes) and can no longer see people/facts that should have been
global — e.g. an agent no longer recognizing someone it has talked to
before, because that Person node got written to `user:alice` instead of
`global`.

This script force-sets e.scope = 'global' on every entity, restoring the
default. It does NOT touch e.pinned (a global-scoped entity can still be
pinned at a narrow scope; that's a separate field and separate decision).

Usage:
    python scripts/unscope_to_global.py                       # prompts for confirmation
    python scripts/unscope_to_global.py --yes                 # skip confirmation
    python scripts/unscope_to_global.py --config path/to/config.yaml
    python scripts/unscope_to_global.py --db path/to/graph.lbug
    python scripts/unscope_to_global.py --dry-run              # count only, no writes

Config resolution (when --config isn't given or doesn't exist): resolved via
utils/instance.py, same as the CLI (--dir / CWD .tinyctx / ~/.tinyctx).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _open(args):
    if args.db:
        kg_path = Path(args.db).expanduser().resolve()
    else:
        from TinyCTX.utils.instance import resolve_instance_dir, config_path_for
        config_path = Path(args.config) if Path(args.config).exists() else config_path_for(resolve_instance_dir())
        if not config_path.exists():
            print(f"[error] Config not found: {config_path.resolve()}")
            sys.exit(1)
        try:
            from TinyCTX.config import load as load_config
            cfg = load_config(str(config_path))
            memory_cfg = cfg.extra.get("memory", {}) if isinstance(cfg.extra, dict) else {}
            # Matches modules/memory/__main__.py's register_runtime(): default
            # is "memory/memory.lbug", resolved relative to data.path unless
            # the configured path is already absolute.
            graph_path_raw = memory_cfg.get("graph_path", "memory/memory.lbug")
            candidate = Path(graph_path_raw)
            data_path = Path(cfg.data.path).expanduser().resolve()
            kg_path = candidate if candidate.is_absolute() else (data_path / candidate).resolve()
        except Exception as e:
            print(f"[error] Failed to load config: {e}")
            sys.exit(1)

    if not kg_path.exists():
        print(f"[error] Graph DB not found: {kg_path}")
        sys.exit(1)

    try:
        from TinyCTX.modules.memory.graph import GraphDatabase
    except ImportError:
        print("[error] ladybug not installed")
        sys.exit(1)

    try:
        graph_database = GraphDatabase(kg_path)
        conn = graph_database.new_read_conn()  # sync Connection; fine for writes too
    except Exception as e:
        print(f"[error] Could not open graph DB: {e}")
        sys.exit(1)

    return kg_path, graph_database, conn


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset every entity's scope to 'global'")
    parser.add_argument("--config",  default="config.yaml")
    parser.add_argument("--db",      default="")
    parser.add_argument("--yes",     action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--dry-run", action="store_true", help="Count affected rows only, no writes")
    args = parser.parse_args()

    kg_path, graph_database, conn = _open(args)
    print(f"db: {kg_path}")

    r = conn.execute("MATCH (e:Entity) WHERE e.scope <> 'global' RETURN count(e)")
    n = r.get_next()[0] if r and r.has_next() else 0

    if n == 0:
        print("No entities are narrowly scoped — nothing to reset.")
        conn.close()
        graph_database.close()
        return

    r2 = conn.execute(
        "MATCH (e:Entity) WHERE e.scope <> 'global' "
        "RETURN e.scope, count(e) ORDER BY count(e) DESC"
    )
    print(f"{n} entities are not scope='global':")
    while r2 and r2.has_next():
        scope, count = r2.get_next()
        print(f"  {scope}: {count}")

    if args.dry_run:
        print("(dry-run — no changes written)")
        conn.close()
        graph_database.close()
        return

    if not args.yes:
        ans = input(f"Set scope='global' on all {n} entities? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted.")
            conn.close()
            graph_database.close()
            return

    conn.execute("MATCH (e:Entity) WHERE e.scope <> 'global' SET e.scope = 'global'")
    print(f"Reset {n} entity/entities to scope='global'.")

    conn.close()
    graph_database.close()


if __name__ == "__main__":
    main()

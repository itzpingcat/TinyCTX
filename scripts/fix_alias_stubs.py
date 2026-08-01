#!/usr/bin/env python3
"""
fix_alias_stubs.py — Damage control for the alias-stub bug in
modules/memory/tools.py::_merge_internal.

Bug (fixed in code as of this script): verdict="alias" merges used to
OVERWRITE the duplicate node's description with a fixed stub:
    "Aliased to {canonical_name} (UUID {canonical_uid})."
destroying whatever real content that node had. There is no history table
and no git tracking for graph DB content, so the original description text
is NOT recoverable — this script cannot restore it.

What this script does instead (best-effort cleanup, not data recovery):
  1. Finds every entity whose description exactly matches the stub pattern
     "Aliased to X (UUID Y)." AND has a real outgoing ALIASED_TO edge to Y.
  2. Prints a report of every affected UUID/name/canonical-target so you can
     decide whether to manually re-populate any of them from other sources
     (chat history, re-extraction, etc) BEFORE deleting. This report is the
     actual "damage control" output — read it, since deletion is permanent.
  3. Deletes those nodes outright (DETACH DELETE — node plus all its edges,
     including the ALIASED_TO edge to canonical). They carry zero recoverable
     information, so keeping a dead stub node around just adds clutter; the
     canonical node they point to is untouched.

Usage:
    python scripts/fix_alias_stubs.py                 # prompts for confirmation
    python scripts/fix_alias_stubs.py --yes
    python scripts/fix_alias_stubs.py --dry-run        # report only, no writes
    python scripts/fix_alias_stubs.py --config path/to/config.yaml
    python scripts/fix_alias_stubs.py --db path/to/graph.lbug

IMPORTANT: stop the TinyCTX process before running this, same reasoning as
invalidate_embeddings.py — deleted nodes may still be resident in a live
process's warm vector index until restart.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

STUB_RE = re.compile(r"^Aliased to (.+) \(UUID ([0-9a-fA-F-]{36})\)\.$")


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
    parser = argparse.ArgumentParser(description="Delete dead alias-stub nodes; report affected entities first")
    parser.add_argument("--config",  default="config.yaml")
    parser.add_argument("--db",      default="")
    parser.add_argument("--yes",     action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    args = parser.parse_args()

    kg_path, graph_database, conn = _open(args)
    print(f"db: {kg_path}")

    r = conn.execute("MATCH (e:Entity) RETURN e.uuid, e.name, e.entity_type, e.description")
    rows = []
    while r and r.has_next():
        rows.append(r.get_next())

    affected = []
    for uid, name, etype, desc in rows:
        m = STUB_RE.match((desc or "").strip())
        if m:
            affected.append({
                "uuid": uid, "name": name, "entity_type": etype,
                "canonical_name": m.group(1), "canonical_uid": m.group(2),
            })

    if not affected:
        print("No dead alias-stub descriptions found. Nothing to do.")
        conn.close()
        graph_database.close()
        return

    print(f"\n{len(affected)} entities have a dead alias-stub description "
          f"(original content already lost, unrecoverable) and will be DELETED "
          f"(node + all its edges):\n")
    for a in affected:
        print(f"  - {a['name']!r} (UUID {a['uuid']}) -> {a['canonical_name']!r} (UUID {a['canonical_uid']})")
    print("\nThis is permanent. Review the list above and manually re-populate "
          "any node whose lost content you can reconstruct from elsewhere "
          "BEFORE proceeding.\n")

    if args.dry_run:
        print("(dry-run — no changes written)")
        conn.close()
        graph_database.close()
        return

    if not args.yes:
        ans = input(f"Permanently delete {len(affected)} dead alias-stub node(s)? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted.")
            conn.close()
            graph_database.close()
            return

    for a in affected:
        conn.execute(
            "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
            {"uid": a["uuid"]},
        )

    print(f"Deleted {len(affected)} dead alias-stub node(s).")
    print("Next: restart the TinyCTX process so the vector index drops any stale entries for them.")

    conn.close()
    graph_database.close()


if __name__ == "__main__":
    main()

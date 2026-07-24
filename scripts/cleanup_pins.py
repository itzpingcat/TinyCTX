#!/usr/bin/env python3
"""
cleanup_pins.py — Interactive audit of pinned entities.

For each pinned entity matching the chosen target, shows its full details
and asks:
  k  keep   — leave pinned:<target> as-is
  u  unpin  — clear pinned_target (entity stays in graph, just not pinned)
  d  delete — remove entity from graph entirely
  q  quit   — stop and checkpoint whatever was done so far

Usage:
    python scripts/cleanup_pins.py                       # global pins (default)
    python scripts/cleanup_pins.py --target global
    python scripts/cleanup_pins.py --target kamie         # a specific user's pins
    python scripts/cleanup_pins.py --config path/to/config.yaml
    python scripts/cleanup_pins.py --db path/to/graph.lbug
    python scripts/cleanup_pins.py --dry-run              # preview only, no writes

Config resolution (when --config isn't given or doesn't exist): resolved via
utils/instance.py, same as the CLI (--dir / CWD .tinyctx / ~/.tinyctx).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


def _ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _get(e: dict, field: str) -> str:
    return str(e.get(f"e.{field}", e.get(field, "")) or "")


def _print_entity(e: dict) -> None:
    pin = f" [pinned:{_get(e, 'pinned_target')}]" if _get(e, 'pinned_target') else ""
    print(f"\n[{_get(e, 'entity_type')}] {_get(e, 'name')}{pin}")
    print(f"  uuid:     {_get(e, 'uuid')}")
    print(f"  priority: {_get(e, 'priority')}")
    print(f"  created:  {_ts(_get(e, 'created_at'))}")
    print(f"  updated:  {_ts(_get(e, 'updated_at'))}")
    desc = _get(e, 'description')
    if desc:
        print(f"  desc:     {desc}")
    for edge in e.get("edges_out", []):
        w = f" w={edge['weight']:.2f}" if edge.get("weight") is not None else ""
        d = f" — {edge['description']}" if edge.get("description") else ""
        print(f"  -> [{edge['relation']}] -> {edge['target_name']} ({edge['target_uuid'][:8]}){w}{d}")
    for edge in e.get("edges_in", []):
        w = f" w={edge['weight']:.2f}" if edge.get("weight") is not None else ""
        d = f" — {edge['description']}" if edge.get("description") else ""
        print(f"  <- [{edge['relation']}] <- {edge['source_name']} ({edge['source_uuid'][:8]}){w}{d}")


def _unpin(conn, uid: str) -> None:
    conn.execute(
        "MATCH (e:Entity {uuid: $uid}) SET e.pinned_target = NULL",
        parameters={"uid": uid},
    )


def _repin_user(conn, uid: str, username: str) -> None:
    conn.execute(
        "MATCH (e:Entity {uuid: $uid}) SET e.pinned_target = $t",
        parameters={"uid": uid, "t": username},
    )


def _delete(conn, uid: str) -> None:
    conn.execute(
        "MATCH (e:Entity {uuid: $uid}) DETACH DELETE e",
        parameters={"uid": uid},
    )


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
            memory_cfg = cfg.extra.get("memory", {})
            kg_path_raw = memory_cfg.get("db_path") if memory_cfg else None
            kg_path = (
                Path(kg_path_raw).expanduser().resolve()
                if kg_path_raw
                else Path(cfg.data.path) / "memory" / "graph.lbug"
            )
        except Exception as e:
            print(f"[error] Failed to load config: {e}")
            sys.exit(1)

    if not kg_path.exists():
        print(f"[error] Graph DB not found: {kg_path}")
        sys.exit(1)

    try:
        from TinyCTX.modules.memory.graph import GraphDatabase, GraphDB
    except ImportError:
        print("[error] ladybug not installed")
        sys.exit(1)

    try:
        graph_database = GraphDatabase(kg_path)
        gdb = GraphDB(graph_database)
        write_conn = graph_database.new_read_conn()
    except Exception as e:
        print(f"[error] Could not open graph DB: {e}")
        sys.exit(1)

    return kg_path, graph_database, gdb, write_conn


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and clean up pinned entities")
    parser.add_argument("--target",  default="global", help="pinned_target to review: 'global' or a username (default: global)")
    parser.add_argument("--config",  default="config.yaml")
    parser.add_argument("--db",      default="")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = parser.parse_args()

    target = args.target

    kg_path, graph_database, gdb, write_conn = _open(args)
    print(f"db: {kg_path}")
    if args.dry_run:
        print("(dry-run mode -- no changes will be written)\n")

    entities = gdb.list_entities()
    pins = [e for e in entities if e.get("pinned_target") == target]
    pins.sort(key=lambda e: -(e.get("priority") or 0))

    if not pins:
        print(f"No entities pinned to '{target}' found.")
        gdb.close()
        write_conn.close()
        graph_database.close()
        return

    total = len(pins)
    print(f"{total} entities pinned to '{target}' to review.\n")
    print("Commands:  k=keep  u=unpin  d=delete  q=quit\n")
    print("-" * 60)

    kept = unpinned = deleted = repinned = 0

    for i, e in enumerate(pins, 1):
        uid = e["uuid"]
        full = gdb.get_entity(uid)
        if not full:
            continue

        _print_entity(full)
        print(f"\n  [{i}/{total}]  k=keep  u=unpin  d=delete  r=repin-user  q=quit")

        choice = ""
        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "q"

            if choice in ("k", "keep"):
                kept += 1
                break
            elif choice in ("u", "unpin"):
                if not args.dry_run:
                    _unpin(write_conn, uid)
                print(f"  unpinned: {_get(full, 'name')}")
                unpinned += 1
                break
            elif choice in ("d", "delete"):
                if not args.dry_run:
                    _delete(write_conn, uid)
                print(f"  deleted:  {_get(full, 'name')}")
                deleted += 1
                break
            elif choice in ("r", "repin", "repin-user"):
                try:
                    username = input("  username > ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("  cancelled")
                    continue
                if not username:
                    print("  ? username cannot be empty")
                    continue
                if not args.dry_run:
                    _repin_user(write_conn, uid, username)
                print(f"  repinned: {_get(full, 'name')} -> user:{username}")
                repinned += 1
                break
            elif choice in ("q", "quit"):
                break
            else:
                print("  ? enter k, u, d, r, or q")

        if choice in ("q", "quit"):
            print("\nStopping early.")
            break

        print("-" * 60)

    print(f"\nDone. kept={kept}  unpinned={unpinned}  repinned={repinned}  deleted={deleted}")

    if not args.dry_run and (unpinned + deleted + repinned) > 0:
        print("Checkpointing...", end=" ", flush=True)
        graph_database.checkpoint()
        print("done.")

    gdb.close()
    write_conn.close()
    graph_database.close()


if __name__ == "__main__":
    main()
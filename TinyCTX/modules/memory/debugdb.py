#!/usr/bin/env python3
"""
debugdb.py — Dump all knowledge graph entities and their edges.

Usage:
    python debugdb.py
    python debugdb.py --config path/to/config.yaml
    python debugdb.py --db path/to/graph.lbug
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


def dump(gdb) -> None:
    all_entities = gdb.list_entities()
    if not all_entities:
        print("(no entities found)")
        return

    print(f"{len(all_entities)} entities\n")

    for e in all_entities:
        uid = e["uuid"]
        pin = " [PINNED]" if e.get("pinned") else ""
        print(f"[{e['entity_type']}] {e['name']}{pin}")
        print(f"  uuid:     {uid}")
        print(f"  priority: {e['priority']}")

        full = gdb.get_entity(uid)
        if full:
            print(f"  created:  {_ts(full.get('e.created_at'))}")
            print(f"  updated:  {_ts(full.get('e.updated_at'))}")
            print(f"  mentions: {full.get('e.mention_count')}")
            desc = full.get("e.description", "")
            if desc:
                print(f"  desc:     {desc}")
            for edge in full.get("edges_out", []):
                w = f"  w={edge['weight']:.2f}" if edge.get("weight") is not None else ""
                d = f"  — {edge['description']}" if edge.get("description") else ""
                print(f"  → [{edge['relation']}] → {edge['target_name']} ({edge['target_uuid'][:8]}){w}{d}")
            for edge in full.get("edges_in", []):
                w = f"  w={edge['weight']:.2f}" if edge.get("weight") is not None else ""
                d = f"  — {edge['description']}" if edge.get("description") else ""
                print(f"  ← [{edge['relation']}] ← {edge['source_name']} ({edge['source_uuid'][:8]}){w}{d}")

        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump all KG entities and edges")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", default="", help="Direct path to graph.lbug file")
    args = parser.parse_args()

    if args.db:
        kg_path = Path(args.db).expanduser().resolve()
    else:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"[error] Config not found: {config_path.resolve()}")
            sys.exit(1)
        try:
            from TinyCTX.config import load as load_config
            cfg = load_config(str(config_path))
            memory_cfg = cfg.extra.get("memory", {})
            kg_path_raw = memory_cfg.get("db_path") if memory_cfg else None
            kg_path = Path(kg_path_raw).expanduser().resolve() if kg_path_raw else cfg.workspace.path / "memory" / "graph.lbug"
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
    except Exception as e:
        print(f"[error] Could not open graph DB: {e}")
        sys.exit(1)

    print(f"db: {kg_path}\n")
    try:
        dump(gdb)
    finally:
        gdb.close()
        graph_database.close()


if __name__ == "__main__":
    main()

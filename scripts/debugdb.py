#!/usr/bin/env python3
"""
debugdb.py — Knowledge graph debug multitool.

Subcommands:
  dump    — list all entities with full edges  (default)
  pinned  — show only pinned entities, grouped by pinned_target
  entity  — inspect a single entity by UUID or name fragment
  stats   — graph statistics summary
  decay   — dry-run decay scoring, shows what the next sweep would delete

Usage:
    python scripts/debugdb.py [subcommand] [--config path] [--db path] [options]

    python scripts/debugdb.py                         # dump all
    python scripts/debugdb.py pinned                  # pinned entities only
    python scripts/debugdb.py entity Kamie            # find + inspect by name
    python scripts/debugdb.py entity --uuid abc123    # inspect by UUID prefix
    python scripts/debugdb.py stats                   # graph stats

DB resolution (same for all subcommands):
  --db path      direct path to graph.lbug
  --config path  path to config.yaml (default: resolved via utils/instance.py,
                  same as the CLI: --dir / CWD .tinyctx / ~/.tinyctx)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _get(e: dict, field: str) -> str:
    return str(e.get(f"e.{field}", e.get(field, "")) or "")


def _print_entity_full(e: dict) -> None:
    """Print a full entity dict (from get_entity) with all fields and edges."""
    pin = f" [pinned:{_get(e, 'pinned_target')}]" if _get(e, 'pinned_target') else ""
    print(f"[{_get(e, 'entity_type')}] {_get(e, 'name')}{pin}")
    print(f"  uuid:     {_get(e, 'uuid')}")
    print(f"  priority: {_get(e, 'priority')}")
    print(f"  created:  {_ts(_get(e, 'created_at'))}")
    print(f"  updated:  {_ts(_get(e, 'updated_at'))}")
    print(f"  mentions: {_get(e, 'mention_count')}")
    last_read = _get(e, 'last_read_at')
    if last_read:
        print(f"  last read: {_ts(last_read)}")
    decay = _get(e, 'decay_score')
    if decay:
        print(f"  decay score: {float(decay):.3f}")
    desc = _get(e, 'description')
    if desc:
        print(f"  desc:     {desc}")
    for edge in e.get("edges_out", []):
        w = f" w={edge['weight']:.2f}" if edge.get("weight") is not None else ""
        d = f" — {edge['description']}" if edge.get("description") else ""
        print(f"  → [{edge['relation']}] → {edge['target_name']} ({edge['target_uuid'][:8]}){w}{d}")
    for edge in e.get("edges_in", []):
        w = f" w={edge['weight']:.2f}" if edge.get("weight") is not None else ""
        d = f" — {edge['description']}" if edge.get("description") else ""
        print(f"  ← [{edge['relation']}] ← {edge['source_name']} ({edge['source_uuid'][:8]}){w}{d}")


# ---------------------------------------------------------------------------
# Subcommand: dump
# ---------------------------------------------------------------------------

def cmd_dump(gdb, args) -> None:
    all_entities = gdb.list_entities()
    if not all_entities:
        print("(no entities found)")
        return

    print(f"{len(all_entities)} entities\n")
    for e in all_entities:
        full = gdb.get_entity(e["uuid"])
        if full:
            _print_entity_full(full)
        print()


# ---------------------------------------------------------------------------
# Subcommand: pinned
# ---------------------------------------------------------------------------

def cmd_pinned(gdb, args) -> None:
    all_entities = gdb.list_entities()
    pinned = [e for e in all_entities if e.get("pinned_target")]

    if not pinned:
        print("(no pinned entities)")
        return

    # Group by pinned_target value
    groups: dict[str, list] = {}
    for e in pinned:
        target = e.get("pinned_target") or "unknown"
        groups.setdefault(target, []).append(e)

    total = len(pinned)
    print(f"{total} pinned entit{'y' if total == 1 else 'ies'} across {len(groups)} target(s)\n")

    for target, entities in sorted(groups.items()):
        print(f"══ pinned_target = '{target}' ({len(entities)}) ══")
        for e in sorted(entities, key=lambda x: -(x.get("priority") or 0)):
            full = gdb.get_entity(e["uuid"])
            if full:
                _print_entity_full(full)
            print()


# ---------------------------------------------------------------------------
# Subcommand: entity
# ---------------------------------------------------------------------------

def cmd_entity(gdb, args) -> None:
    uid = getattr(args, "uuid", None)
    name_frag = " ".join(args.name) if args.name else None

    if uid:
        # Match UUID prefix against all entities
        all_e = gdb.list_entities()
        matches = [e for e in all_e if e["uuid"].startswith(uid)]
        if not matches:
            print(f"[error] no entity with UUID starting with '{uid}'")
            return
        for e in matches:
            full = gdb.get_entity(e["uuid"])
            if full:
                _print_entity_full(full)
            print()

    elif name_frag:
        found = gdb.find_entity(name=name_frag)
        if not found:
            print(f"(no entity found matching '{name_frag}')")
            return
        print(f"{len(found)} match(es) for '{name_frag}':\n")
        for e in found:
            full = gdb.get_entity(e["uuid"])
            if full:
                _print_entity_full(full)
            print()

    else:
        print("[error] provide a name fragment or --uuid prefix")


# ---------------------------------------------------------------------------
# Subcommand: stats
# ---------------------------------------------------------------------------

def cmd_stats(gdb, args) -> None:
    s = gdb.get_stats()
    print(f"entities:         {s['entity_count']}")
    print(f"active edges:     {s['active_edge_count']}")
    print(f"superseded edges: {s['superseded_edge_count']}")
    print(f"pinned:           {s['pinned_count']}")
    print(f"embedded:         {s['embedded_count']}")
    print(f"avg priority:     {s['avg_priority']}")
    if s["by_type"]:
        print("\nby type:")
        for t, n in s["by_type"].items():
            print(f"  {t:<20} {n}")
    if s["top_mentioned"]:
        print("\ntop mentioned:")
        for m in s["top_mentioned"]:
            print(f"  {m['mention_count']:>4}x  [{m['entity_type']}] {m['name']}")


# ---------------------------------------------------------------------------
# Subcommand: decay
# ---------------------------------------------------------------------------

def cmd_decay(gdb, args, graph_database=None, memory_cfg=None) -> None:
    """
    Dry-run the decay scoring pass and print every non-pinned entity sorted
    by score ascending (most decay-prone first). Does not write decay_score
    or delete anything — read-only inspection of what the next scheduled
    sweep would do.

    memory_cfg should be the fully resolved memory config (defaults merged
    with config.yaml overrides) so the printed threshold matches what the
    real scheduled sweep would actually use, not just the hardcoded defaults.
    """
    import asyncio
    from TinyCTX.modules.memory.decay import compute_decay_scores

    cfg = memory_cfg if memory_cfg is not None else {}
    threshold = float(cfg.get("decay_threshold", 0.2))

    assert graph_database is not None
    conn = graph_database.new_async_write_conn()
    try:
        scores = asyncio.run(compute_decay_scores(conn, cfg))
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not scores:
        print("(no non-pinned entities to score)")
        return

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"])
    below = sum(1 for _, r in ranked if r["score"] < threshold)

    print(f"{len(ranked)} non-pinned entities scored — threshold {threshold:.2f} ({below} below)\n")
    for uid, r in ranked:
        flag = "  [WOULD DELETE]" if r["score"] < threshold else ""
        f = r["factors"]
        print(f"{r['score']:.3f}  [{r['entity_type']}] {r['name']} ({uid[:8]}){flag}")
        print(
            f"        priority={f['priority']:.2f} distance={f['distance']:.2f} "
            f"edges={f['edges']:.2f} mentions={f['mentions']:.2f} recency={f['recency']:.2f}"
        )


# ---------------------------------------------------------------------------
# DB open helper
# ---------------------------------------------------------------------------

def _find_config(given: str) -> Path:
    """Resolve config path: use given if it exists, else resolve via the instance dir."""
    p = Path(given)
    if p.exists():
        return p
    from TinyCTX.utils.instance import resolve_instance_dir, config_path_for
    return config_path_for(resolve_instance_dir())


def _resolve_memory_cfg(args) -> dict:
    """
    Resolve the memory config the same way register_runtime does: defaults
    from EXTENSION_META merged with config.yaml's extra.memory overrides.
    Falls back to defaults only when --db was given directly (no config.yaml
    to read) or when config loading fails.
    """
    from TinyCTX.modules.memory import EXTENSION_META
    defaults = EXTENSION_META.get("default_config", {})

    if args.db:
        # Direct DB path, no config.yaml in play — defaults only.
        return dict(defaults)

    config_path = _find_config(args.config)
    if not config_path.exists():
        return dict(defaults)

    try:
        from TinyCTX.config import load as load_config
        cfg = load_config(str(config_path))
        overrides = cfg.extra.get("memory", {}) if hasattr(cfg, "extra") and isinstance(cfg.extra, dict) else {}
    except Exception:
        return dict(defaults)

    return {**defaults, **overrides}


def _open_db(args):
    if args.db:
        kg_path = Path(args.db).expanduser().resolve()
    else:
        config_path = _find_config(args.config)
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
    except Exception as e:
        print(f"[error] Could not open graph DB: {e}")
        sys.exit(1)

    return kg_path, graph_database, gdb


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

SUBCOMMANDS = {
    "dump":   cmd_dump,
    "pinned": cmd_pinned,
    "entity": cmd_entity,
    "stats":  cmd_stats,
    "decay":  cmd_decay,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Knowledge graph debug multitool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--db", default="", help="Direct path to graph.lbug")

    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("dump",   help="List all entities with edges (default)")
    subparsers.add_parser("pinned", help="Show pinned entities grouped by pinned_target")
    subparsers.add_parser("stats",  help="Graph statistics summary")
    subparsers.add_parser("decay",  help="Dry-run decay scoring — shows what the next sweep would delete")

    ep = subparsers.add_parser("entity", help="Inspect a single entity by name or UUID")
    ep.add_argument("name", nargs="*", help="Name fragment to search for")
    ep.add_argument("--uuid", default="", help="UUID prefix to match")

    args = parser.parse_args()

    cmd_fn = SUBCOMMANDS.get(args.subcommand or "dump", cmd_dump)

    kg_path, graph_database, gdb = _open_db(args)
    print(f"db: {kg_path}\n")
    try:
        if cmd_fn is cmd_decay:
            memory_cfg = _resolve_memory_cfg(args)
            cmd_fn(gdb, args, graph_database=graph_database, memory_cfg=memory_cfg)
        else:
            cmd_fn(gdb, args)
    finally:
        gdb.close()
        graph_database.close()


if __name__ == "__main__":
    main()

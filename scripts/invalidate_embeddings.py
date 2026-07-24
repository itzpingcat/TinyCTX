#!/usr/bin/env python3
"""
invalidate_embeddings.py — Force every entity's embedding to be recomputed.

embed_hash is a pure content hash: sha256(embed_content). It has no idea
whether the *backend* that produced the stored embedding was actually
working correctly. If embeddings were written while the embedder was
broken (e.g. a bad --pooling flag silently producing near-constant
vectors), embed_hash still matches the current content, so the dirty-set
query in deduper.refresh_embeddings() ('embed_hash = "" OR IS NULL') never
picks those rows up again — the garbage vectors sit there forever,
poisoning cosine similarity for every dedup cycle.

This script zeroes e.embedding and e.embed_hash for every entity, which
marks the whole graph dirty. The next refresh_embeddings() pass (runs
automatically as part of run_dedup_cycle) re-embeds everything through
whatever embedder is currently configured.

IMPORTANT: stop the TinyCTX process before running this. warm_index()
loads any row with e.embedding IS NOT NULL into the in-memory vector index
on startup, with no regard for embed_hash — if the process is running and
restarts (or already has a warm index) between this script and the next
dedup cycle, stale vectors can still be in play for a while. Running this
with the process stopped, then starting it, guarantees the index only
ever sees NULL until refresh_embeddings repopulates it for real.

Usage:
    python scripts/invalidate_embeddings.py                       # prompts for confirmation
    python scripts/invalidate_embeddings.py --yes                 # skip confirmation
    python scripts/invalidate_embeddings.py --config path/to/config.yaml
    python scripts/invalidate_embeddings.py --db path/to/graph.lbug
    python scripts/invalidate_embeddings.py --dry-run              # count only, no writes

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
    parser = argparse.ArgumentParser(description="Force every entity to re-embed on the next dedup cycle")
    parser.add_argument("--config",  default="config.yaml")
    parser.add_argument("--db",      default="")
    parser.add_argument("--yes",     action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--dry-run", action="store_true", help="Count affected rows only, no writes")
    args = parser.parse_args()

    kg_path, graph_database, conn = _open(args)
    print(f"db: {kg_path}")

    r = conn.execute("MATCH (e:Entity) WHERE e.embedding IS NOT NULL RETURN count(e)")
    n = r.get_next()[0] if r and r.has_next() else 0

    if n == 0:
        print("No entities currently have an embedding — nothing to invalidate.")
        conn.close()
        graph_database.close()
        return

    print(f"{n} entities currently have an embedding.")
    if args.dry_run:
        print("(dry-run — no changes written)")
        conn.close()
        graph_database.close()
        return

    if not args.yes:
        ans = input(f"Zero embedding + embed_hash on all {n}? This forces a full re-embed. [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted.")
            conn.close()
            graph_database.close()
            return

    conn.execute(
        "MATCH (e:Entity) WHERE e.embedding IS NOT NULL "
        "SET e.embedding = NULL, e.embed_hash = ''"
    )
    print(f"Invalidated {n} embedding(s). Every entity is now dirty.")
    print("Next: make sure the TinyCTX process is stopped (if it wasn't already), "
          "then start it back up — the next dedup cycle's refresh_embeddings pass "
          "will re-embed everything through the currently configured embedder.")

    conn.close()
    graph_database.close()


if __name__ == "__main__":
    main()

"""
modules/memory/migrate.py

One-shot migration from the old v1 graph (graph.lbug) to the v2 graph
(memory.lbug). Not silently destructive:

  * runs only when graph.lbug exists and memory.lbug does not
  * `--dry-run` reports what would move, writes nothing
  * on success the old file is RENAMED to graph.lbug.migrated.bak (not deleted)
  * `--purge` deletes the .bak backup once you've confirmed the new graph is good

Field mapping (v1 -> v2):
  name, entity_type, description      -> copied 1:1
  pinned_target ("global"|<user>)     -> pinned ("global" | "user:<user>")
  priority                            -> DROPPED
  mention_count                       -> mention (DOUBLE)
  created_at / updated_at             -> copied
  embedding / embed_hash              -> copied IF embed_content rendering matches,
                                          else embed_hash="" (lazy re-embed)
  graph_* columns                     -> DROPPED
  scope                               -> everything -> "global" (nothing that was
                                          globally visible becomes invisible)
  relations: relation, weight         -> copied; updated_at = created_at
  relations with superseded_at set    -> SKIPPED (dead soft-deleted edges)
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from TinyCTX.modules.memory.graph import embed_content_for, embed_hash

logger = logging.getLogger(__name__)

BAK_SUFFIX = ".migrated.bak"


# ---------------------------------------------------------------------------
# Pure mapping (unit-tested)
# ---------------------------------------------------------------------------

def map_pinned(pinned_target) -> str:
    """v1 pinned_target -> v2 pinned. 'global' stays; a bare username becomes
    'user:<name>'; null/empty -> unpinned."""
    if not pinned_target:
        return ""
    if pinned_target == "global":
        return "global"
    return f"user:{pinned_target}"


def map_entity(old: dict) -> dict:
    """Map a v1 Entity row dict to a v2 Entity dict. Everything -> scope 'global'.
    Preserves the embedding only when the v2 embed_content rendering matches the
    stored v1 hash; otherwise marks it stale for lazy re-embed."""
    name = old.get("name") or ""
    etype = old.get("entity_type") or ""
    desc = old.get("description") or ""
    content = embed_content_for(name, etype, desc)

    old_emb = old.get("embedding")
    old_hash = old.get("embed_hash") or ""
    if old_emb and old_hash and old_hash == embed_hash(content):
        embedding, e_hash = old_emb, old_hash
    else:
        embedding, e_hash = None, ""  # lazy re-embed

    return {
        "uuid": old.get("uuid"),
        "name": name,
        "entity_type": etype,
        "description": desc,
        "scope": "global",
        "pinned": map_pinned(old.get("pinned_target")),
        "mention": float(old.get("mention_count") or 0),
        "created_at": old.get("created_at"),
        "updated_at": old.get("updated_at"),
        "embed_content": content,
        "embed_hash": e_hash,
        "embedding": embedding,
    }


def should_skip_edge(superseded_at) -> bool:
    """v1 soft-deleted edges (superseded_at set) are dead — skip them."""
    return superseded_at is not None


# ---------------------------------------------------------------------------
# Migration driver (needs ladybug; not runnable without the engine)
# ---------------------------------------------------------------------------

def migrate(old_path: Path, new_path: Path, *, dry_run: bool = False) -> dict:
    """Stream v1 -> v2. Returns a summary dict. Raises on verification failure."""
    import ladybug
    from TinyCTX.modules.memory.graph import GraphDatabase

    if not old_path.exists():
        return {"status": "no-op", "reason": "old graph not found"}
    if new_path.exists():
        return {"status": "no-op", "reason": "new graph already exists"}

    old_db = ladybug.Database(str(old_path))
    old_conn = ladybug.Connection(old_db)

    # read entities
    ents_in = []
    r = old_conn.execute("MATCH (e:Entity) RETURN e.*")
    cols = r.get_column_names() if r else []
    while r and r.has_next():
        row = dict(zip(cols, r.get_next()))
        ents_in.append({k.split(".", 1)[-1]: v for k, v in row.items()})

    # read edges
    edges_in = []
    r = old_conn.execute(
        "MATCH (a:Entity)-[rel:Relation]->(b:Entity) "
        "RETURN a.uuid, b.uuid, rel.relation, rel.weight, rel.created_at, rel.superseded_at"
    )
    while r and r.has_next():
        a, b, rl, w, ca, sup = r.get_next()
        edges_in.append({"a": a, "b": b, "relation": rl, "weight": w, "created_at": ca, "superseded_at": sup})

    mapped = [map_entity(e) for e in ents_in]
    kept_edges = [e for e in edges_in if not should_skip_edge(e["superseded_at"])]

    summary = {
        "status": "dry-run" if dry_run else "migrated",
        "entities_in": len(ents_in),
        "entities_out": len(mapped),
        "edges_in": len(edges_in),
        "edges_out": len(kept_edges),
        "edges_dropped": len(edges_in) - len(kept_edges),
    }
    old_conn.close()
    old_db.close()
    if dry_run:
        return summary

    # write v2
    gdb = GraphDatabase(new_path)
    conn = gdb.new_read_conn()
    for e in mapped:
        conn.execute("CREATE (n:Entity {uuid: $uid})", parameters={"uid": e["uuid"]})
        for field in ("name", "entity_type", "description", "scope", "pinned", "mention",
                      "created_at", "updated_at", "embed_content", "embed_hash", "embedding"):
            conn.execute(
                f"MATCH (n:Entity) WHERE n.uuid = $uid SET n.{field} = $v",
                parameters={"uid": e["uuid"], "v": e[field]},
            )
    for e in kept_edges:
        conn.execute(
            "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
            "CREATE (a)-[:Relation {relation:$rel, weight:$w, created_at:$ca, updated_at:$ca}]->(b)",
            parameters={"a": e["a"], "b": e["b"], "rel": e["relation"],
                        "w": e["weight"], "ca": e["created_at"]},
        )
    # verify
    r = conn.execute("MATCH (e:Entity) RETURN count(e)")
    out_count = r.get_next()[0] if r and r.has_next() else 0
    conn.close()
    gdb.close()
    if out_count != len(mapped):
        raise RuntimeError(f"verification failed: wrote {out_count}, expected {len(mapped)}")

    bak = old_path.with_suffix(old_path.suffix + BAK_SUFFIX)
    old_path.rename(bak)
    summary["backup"] = str(bak)
    return summary


def purge_backup(old_path: Path) -> bool:
    bak = old_path.with_suffix(old_path.suffix + BAK_SUFFIX)
    if bak.exists():
        bak.unlink()
        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate v1 graph.lbug -> v2 memory.lbug")
    ap.add_argument("data_dir", type=Path, help="dir containing memory/graph.lbug")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge", action="store_true", help="delete the .bak after a confirmed migration")
    args = ap.parse_args()

    old_path = args.data_dir / "memory" / "graph.lbug"
    new_path = args.data_dir / "memory" / "memory.lbug"

    logging.basicConfig(level=logging.INFO)
    if args.purge:
        print("purged" if purge_backup(old_path) else "no backup to purge")
        return
    summary = migrate(old_path, new_path, dry_run=args.dry_run)
    print(summary)


if __name__ == "__main__":
    main()

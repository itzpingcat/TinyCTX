"""
modules/memory/deduper.py

Two graph-maintenance jobs:

1. refresh_embeddings() — the embedding pass. Drains the dirty set (rows with
   empty embed_hash), embeds their embed_content, writes embedding + embed_hash,
   and upserts into the in-memory VectorIndex.

2. run_dedup_cycle() — semantic dedup. Generates candidate pairs from the vector
   index (cosine >= threshold), drops cached-distinct and already-aliased pairs,
   groups survivors via greedy clique-edge-cover into batches, asks the LLM to
   confirm duplicates per batch, and merges confirmed ones (shared merge helper
   with the memory_merge_into tool). Confirmed-distinct pairs are cached in a
   sqlite sidecar so we never re-spend on them.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from TinyCTX.modules.memory import tools as _tools
from TinyCTX.modules.memory.graph import cosine_similarity, embed_hash

logger = logging.getLogger(__name__)
_PROMPTS = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# Embedding pass
# ---------------------------------------------------------------------------

async def refresh_embeddings(cfg, conn, write_lock, embedder, graph_db) -> int:
    """Embed all dirty rows (embed_hash == ''). Returns count embedded."""
    if embedder is None:
        return 0
    r = graph_db.safe_execute(
        "MATCH (e:Entity) WHERE e.embed_hash = '' OR e.embed_hash IS NULL "
        "RETURN e.uuid, e.embed_content"
    )
    dirty: list[tuple[str, str]] = []
    while r and r.has_next():
        uid, content = r.get_next()
        dirty.append((uid, content or ""))
    if not dirty:
        return 0

    doc_tmpl = cfg.get("embed_document_template", "{text}")
    n = 0
    for uid, content in dirty:
        try:
            vec = await embedder.embed_one(doc_tmpl.format(text=content), priority=15)
        except Exception as exc:
            logger.warning("[memory/deduper] embed failed for %s: %s", uid[:8], exc)
            continue
        h = embed_hash(content)
        async with write_lock:
            await conn.execute(
                "MATCH (e:Entity) WHERE e.uuid = $uid SET e.embedding = $v, e.embed_hash = $h",
                parameters={"uid": uid, "v": vec, "h": h},
            )
        graph_db.vector_index.upsert(uid, vec)
        n += 1
    logger.info("[memory/deduper] embedded %d dirty row(s)", n)
    return n


# ---------------------------------------------------------------------------
# Pure algorithms (unit-tested)
# ---------------------------------------------------------------------------

def candidate_pairs(vectors: dict, threshold: float) -> list[tuple[str, str]]:
    """All unordered uuid pairs whose cosine >= threshold. O(n^2), fine at KG
    scale. Returns pairs with a < b for stable identity."""
    uids = list(vectors.keys())
    pairs: list[tuple[str, str]] = []
    for i in range(len(uids)):
        for j in range(i + 1, len(uids)):
            a, b = uids[i], uids[j]
            if cosine_similarity(vectors[a], vectors[b]) >= threshold:
                pairs.append((a, b) if a < b else (b, a))
    return pairs


def clique_edge_cover(pairs: list[tuple[str, str]], max_size: int) -> list[list[str]]:
    """
    Greedy clique-edge-cover: group nodes so every candidate pair is contained in
    some returned group, each group is a near-clique (all members pairwise
    connected), and no group exceeds max_size. Every edge is covered at least
    once.
    """
    adj: dict[str, set[str]] = {}
    for a, b in pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    uncovered = {tuple(sorted(p)) for p in pairs}
    groups: list[list[str]] = []

    while uncovered:
        a, b = next(iter(uncovered))
        group = [a, b]
        # grow the clique with common neighbours
        candidates = adj[a] & adj[b]
        for c in candidates:
            if len(group) >= max_size:
                break
            if all(c in adj.get(g, set()) for g in group):
                group.append(c)
        # remove all now-covered edges among group members
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                uncovered.discard(tuple(sorted((group[i], group[j]))))
        groups.append(group)
    return groups


def parse_merge_ops(text: str) -> list[dict]:
    """Parse an LLM JSON response of merge operations. Tolerant of surrounding
    prose / code fences. Returns [{canonical, duplicate, merged_description,
    verdict}]."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        ops = json.loads(text[start:end + 1])
    except ValueError:
        return []
    out = []
    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        if op.get("canonical") and op.get("duplicate"):
            out.append({
                "canonical": op["canonical"],
                "duplicate": op["duplicate"],
                "merged_description": op.get("merged_description", ""),
                "verdict": op.get("verdict", "duplicate"),
            })
    return out


# ---------------------------------------------------------------------------
# Dedup cache (sqlite sidecar in data dir)
# ---------------------------------------------------------------------------

class DedupCache:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS distinct_pairs (uuid_a TEXT, uuid_b TEXT, PRIMARY KEY (uuid_a, uuid_b))"
        )
        self._con.commit()

    def is_cached(self, a: str, b: str) -> bool:
        a, b = (a, b) if a < b else (b, a)
        cur = self._con.execute(
            "SELECT 1 FROM distinct_pairs WHERE uuid_a = ? AND uuid_b = ?", (a, b)
        )
        return cur.fetchone() is not None

    def mark_distinct(self, a: str, b: str) -> None:
        a, b = (a, b) if a < b else (b, a)
        self._con.execute("INSERT OR IGNORE INTO distinct_pairs (uuid_a, uuid_b) VALUES (?, ?)", (a, b))
        self._con.commit()

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dedup cycle
# ---------------------------------------------------------------------------

async def run_dedup_cycle(cfg, data_dir, conn, write_lock, llm, embedder, graph_db, agent_logger) -> None:
    from TinyCTX.ai import TextDelta, LLMError

    await refresh_embeddings(cfg, conn, write_lock, embedder, graph_db)

    threshold = float(cfg.get("similarity_threshold", 0.90))
    batch_count = int(cfg.get("dedup_batch_count", 8))

    vectors = dict(graph_db.vector_index._vecs)  # snapshot
    if len(vectors) < 2:
        return
    cache = DedupCache(Path(data_dir) / "dedup_cache.db")
    try:
        pairs = [p for p in candidate_pairs(vectors, threshold) if not cache.is_cached(*p)]
        # drop already-aliased pairs
        pairs = [p for p in pairs if not _is_aliased(graph_db, *p)]
        if not pairs:
            return
        groups = clique_edge_cover(pairs, batch_count)

        for group in groups:
            ents = [graph_db.get_entity_slim(u, None) for u in group]
            ents = [e for e in ents if e]
            if len(ents) < 2:
                continue
            prompt = _read("dedup_group_user.txt").format(entities=_render_group(ents))
            system = _read("dedup_system.txt")
            text_chunks: list[str] = []
            async for event in llm.stream(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                tools=[], priority=15,
            ):
                if isinstance(event, TextDelta):
                    text_chunks.append(event.text)
                elif isinstance(event, LLMError):
                    break
            ops = parse_merge_ops("".join(text_chunks))
            confirmed = {(o["canonical"], o["duplicate"]) for o in ops}
            for op in ops:
                c = graph_db.get_entity_slim(op["canonical"], None)
                d = graph_db.get_entity_slim(op["duplicate"], None)
                if c and d and c["uuid"] != d["uuid"]:
                    async with write_lock:
                        await _tools._merge_internal(c, d, op["merged_description"] or c["description"], op["verdict"])
            # cache the group's pairs that were NOT merged as distinct
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if (a, b) not in confirmed and (b, a) not in confirmed:
                        cache.mark_distinct(a, b)
    finally:
        cache.close()


def _is_aliased(graph_db, a: str, b: str) -> bool:
    r = graph_db.safe_execute(
        "MATCH (x:Entity {uuid:$a})-[r:Relation {relation:'ALIASED_TO'}]->(y:Entity {uuid:$b}) RETURN 1 LIMIT 1",
        {"a": a, "b": b},
    )
    if r and r.has_next():
        return True
    r = graph_db.safe_execute(
        "MATCH (x:Entity {uuid:$b})-[r:Relation {relation:'ALIASED_TO'}]->(y:Entity {uuid:$a}) RETURN 1 LIMIT 1",
        {"a": a, "b": b},
    )
    return bool(r and r.has_next())


def _render_group(ents: list[dict]) -> str:
    return "\n".join(f"- UUID {e['uuid']} [{e['entity_type']}] {e['name']}: {e['description']}" for e in ents)


def _read(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")

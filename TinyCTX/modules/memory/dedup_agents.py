"""
modules/memory/dedup_agents.py

Deduplication logic for the knowledge librarian.
Extracted from librarian_agents.py.

Cache backend: SQLite (memory/dedup_cache.db) instead of JSON.
Schema: single table `distinct_pairs (uuid_a TEXT, uuid_b TEXT, PRIMARY KEY (uuid_a, uuid_b))`
where uuid_a < uuid_b (lexicographic) to keep pairs canonical.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import sqlite3
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# SQLite-backed distinct-pair cache
# ---------------------------------------------------------------------------

def _db_path(workspace_path: Path) -> Path:
    return workspace_path / "memory" / "dedup_cache.db"


def _open_db(workspace_path: Path) -> sqlite3.Connection:
    path = _db_path(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute(
        "CREATE TABLE IF NOT EXISTS distinct_pairs "
        "(uuid_a TEXT NOT NULL, uuid_b TEXT NOT NULL, PRIMARY KEY (uuid_a, uuid_b))"
    )
    con.commit()
    return con


def _canonical_pair(uid_a: str, uid_b: str) -> tuple[str, str]:
    return (uid_a, uid_b) if uid_a < uid_b else (uid_b, uid_a)


def _load_distinct_cache(workspace_path: Path) -> set[frozenset]:
    try:
        con = _open_db(workspace_path)
        rows = con.execute("SELECT uuid_a, uuid_b FROM distinct_pairs").fetchall()
        con.close()
        return {frozenset(row) for row in rows}
    except Exception:
        logger.exception("[dedup_cache] failed to load cache")
        return set()


def _add_to_cache(workspace_path: Path, uid_a: str, uid_b: str) -> None:
    """Incrementally insert a single pair."""
    try:
        con = _open_db(workspace_path)
        a, b = _canonical_pair(uid_a, uid_b)
        con.execute(
            "INSERT OR IGNORE INTO distinct_pairs (uuid_a, uuid_b) VALUES (?, ?)", (a, b)
        )
        con.commit()
        con.close()
    except Exception:
        logger.exception("[dedup_cache] failed to add pair (%s, %s)", uid_a, uid_b)


def _invalidate_cache_for(cache: set[frozenset], uid: str) -> None:
    to_remove = {pair for pair in cache if uid in pair}
    cache -= to_remove


def _invalidate_db_for(workspace_path: Path, uid: str) -> None:
    try:
        con = _open_db(workspace_path)
        con.execute(
            "DELETE FROM distinct_pairs WHERE uuid_a = ? OR uuid_b = ?", (uid, uid)
        )
        con.commit()
        con.close()
    except Exception:
        logger.exception("[dedup_cache] failed to invalidate uid %s", uid)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dedup_response(raw: str) -> list[dict]:
    raw = _re.sub(r"^```json?\s*", "", raw.strip())
    raw = _re.sub(r"\s*```$", "", raw).strip()
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"Expected list or dict, got {type(parsed).__name__}")
    return parsed


async def _aset(conn, uid: str, field: str, value):
    return await conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


# ---------------------------------------------------------------------------
# Pivot-based partitioning (Ailon-Charikar-Newman correlation clustering)
# ---------------------------------------------------------------------------

def _pivot_partition(
    candidates: list[tuple[dict, dict, float]],
    batch_size: int,
) -> list[list[dict]]:
    """
    Partition candidate nodes into groups using the Pivot algorithm, a
    3-approximation for correlation clustering (Ailon, Charikar, Newman 2008).

    Every node in a group has a direct candidate edge to the pivot that anchored
    it — unlike connected-component grouping, there are no transitively-included
    bystanders.  This keeps LLM batches semantically tight.

    Algorithm:
      1. Shuffle nodes (randomised pivot gives the approximation guarantee).
      2. Pick the first unprocessed node as pivot.
      3. Cluster = pivot + all its unprocessed direct neighbours.
      4. Emit the cluster (splitting into batch_size chunks if needed).
      5. Remove all emitted nodes and repeat.

    Splitting an oversized cluster is safe because the cluster is already dense
    (every node has a direct edge to the pivot), so any contiguous slice still
    contains high-quality candidate pairs.
    """
    import random

    by_uuid: dict[str, dict] = {}
    adjacency: dict[str, set[str]] = defaultdict(set)

    for ea, eb, _ in candidates:
        uid_a, uid_b = ea["e.uuid"], eb["e.uuid"]
        by_uuid[uid_a] = ea
        by_uuid[uid_b] = eb
        adjacency[uid_a].add(uid_b)
        adjacency[uid_b].add(uid_a)

    order = list(by_uuid.keys())
    random.shuffle(order)

    processed: set[str] = set()
    result: list[list[dict]] = []

    for pivot in order:
        if pivot in processed:
            continue
        # Cluster: pivot + unprocessed direct neighbours, pivot first
        cluster = [pivot] + [
            u for u in adjacency[pivot] if u not in processed and u != pivot
        ]
        processed.update(cluster)
        # Split into batch_size chunks; every chunk contains the pivot (first
        # element) only in the first chunk, but remaining chunks are still
        # dense because all their members are direct neighbours of the pivot.
        for i in range(0, len(cluster), batch_size):
            result.append([by_uuid[u] for u in cluster[i:i + batch_size]])

    return result


# ---------------------------------------------------------------------------
# Edge dedup — delete duplicate active edges
# ---------------------------------------------------------------------------

async def run_edge_dedup(
    conn,
    write_lock: asyncio.Lock,
    agent_logger: logging.Logger,
) -> None:
    """
    Find and delete duplicate active edges.

    A duplicate is defined as two or more active (superseded_at IS NULL) edges
    sharing the same (source uuid, target uuid, relation) triple. For each
    such group, the most recently created edge is kept and the rest are deleted.
    """
    logger.debug("[memory/librarian] edge dedup starting")
    try:
        r = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            "WHERE r.superseded_at IS NULL "
            "RETURN a.uuid, b.uuid, r.relation, r.created_at"
        )

        groups: dict[tuple, list[tuple]] = defaultdict(list)
        while r.has_next():
            row = r.get_next()
            src, tgt, rel, created_at = row
            groups[(src, tgt, rel)].append((created_at or 0.0,))

        to_delete: list = []
        for (src, tgt, rel), edges in groups.items():
            if len(edges) <= 1:
                continue
            edges.sort(key=lambda x: x[0], reverse=True)
            to_delete.extend((src, tgt, rel, ca) for (ca,) in edges[1:])

        if not to_delete:
            agent_logger.debug("[edge dedup] no duplicate edges found")
            return

        logger.info("[memory/librarian] edge dedup: deleting %d duplicate edge(s)", len(to_delete))
        agent_logger.info("[edge dedup] deleting %d duplicate edges", len(to_delete))

        async with write_lock:
            for (src, tgt, rel, created_at) in to_delete:
                try:
                    await conn.execute(
                        "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
                        "WHERE a.uuid = $src AND b.uuid = $tgt "
                        "AND r.relation = $rel AND r.created_at = $ca "
                        "DELETE r",
                        parameters={"src": src, "tgt": tgt, "rel": rel, "ca": created_at},
                    )
                except Exception as exc:
                    logger.warning(
                        "[memory/librarian] edge dedup: failed to delete edge %s->%s [%s] @ %s: %s",
                        src, tgt, rel, created_at, exc,
                    )

        logger.debug("[memory/librarian] edge dedup complete")
    except Exception:
        logger.exception("[memory/librarian] edge dedup error")


# ---------------------------------------------------------------------------
# Dedup cycle (public entry point)
# ---------------------------------------------------------------------------

async def run_dedup_cycle(
    cfg: dict,
    workspace_path: Path,
    conn,
    write_lock: asyncio.Lock,
    llm,
    embedder,
    agent_logger: logging.Logger,
) -> None:
    """
    Refresh graph embeddings for stale entities, then cluster near-duplicate
    candidates into connected components and evaluate each component with a
    single LLM call.

    Thrash mitigations
    ------------------
    1. Neighbourhood-aware embeddings (name + description + 1-hop edges).
    2. Stale-only comparison: only pairs where >= 1 side was re-embedded.
    3. SQLite distinct-pair cache: confirmed-distinct pairs persist across restarts.
    """
    logger.debug("[memory/librarian] dedup cycle starting")
    try:
        from TinyCTX.modules.memory.graph import (
            embed_content_with_edges, embed_hash, cosine_similarity,
        )

        threshold   = float(cfg.get("similarity_threshold", 0.85))
        batch_size  = int(cfg.get("dedup_batch_count", 6))
        doc_template = cfg.get("embed_document_template", "{text}")

        r = await conn.execute(
            "MATCH (e:Entity) RETURN e.uuid, e.name, e.description, e.entity_type, "
            "e.graph_embed_model, e.graph_embed_hash, e.graph_embedding, e.embedding"
        )
        col_names = r.get_column_names()
        entities  = []
        while r.has_next():
            entities.append(dict(zip(col_names, r.get_next())))

        if len(entities) < 2:
            logger.debug("[memory/librarian] dedup: fewer than 2 entities, skipping")
            return

        distinct_cache = _load_distinct_cache(workspace_path)

        edges_by_uuid: dict[str, list[dict]] = {e["e.uuid"]: [] for e in entities}
        er = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            "WHERE r.superseded_at IS NULL "
            "RETURN a.uuid, r.relation, b.name"
        )
        while er.has_next():
            row = er.get_next()
            src_uid, relation, target_name = row[0], row[1], row[2]
            if src_uid in edges_by_uuid:
                edges_by_uuid[src_uid].append({"relation": relation, "target_name": target_name})

        graph_embed_model_name = getattr(embedder, "model", "")

        # Name-based candidates: same name (case-insensitive). Computed before
        # the stale-embedding check below, since an exact name collision is a
        # duplicate signal on its own and shouldn't depend on whether anyone's
        # embedding happens to be out of date.
        pairs_seen: set[frozenset] = set()
        candidates: list[tuple[dict, dict, float]] = []

        by_name: dict[str, list[dict]] = {}
        for e in entities:
            key = (e["e.name"] or "").strip().lower()
            if key:
                by_name.setdefault(key, []).append(e)
        for group in by_name.values():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    ea, eb = group[i], group[j]
                    pair_key = frozenset([ea["e.uuid"], eb["e.uuid"]])
                    if pair_key not in pairs_seen:
                        pairs_seen.add(pair_key)
                        candidates.append((ea, eb, 1.0))

        stale: list[dict] = []
        for e in entities:
            uid   = e["e.uuid"]
            edges = edges_by_uuid.get(uid, [])
            expected_hash = embed_hash(
                embed_content_with_edges(
                    e["e.name"], e["e.description"], edges, doc_template=doc_template
                )
            )
            if (
                not e["e.graph_embedding"]
                or e["e.graph_embed_model"] != graph_embed_model_name
                or e["e.graph_embed_hash"] != expected_hash
            ):
                stale.append(e)

        if stale:
            agent_logger.info("[dedup] refreshing %d stale graph embedding(s)", len(stale))
            logger.debug("[memory/librarian] dedup: refreshing %d stale graph embedding(s)", len(stale))
            for e in stale:
                _invalidate_cache_for(distinct_cache, e["e.uuid"])
                _invalidate_db_for(workspace_path, e["e.uuid"])

            texts: list[str] = [
                embed_content_with_edges(
                    e["e.name"],
                    e["e.description"],
                    edges_by_uuid.get(e["e.uuid"], []),
                    doc_template=doc_template,
                )
                for e in stale
            ]
            vectors = await embedder.embed(texts, priority=15)
            async with write_lock:
                for e, vec, txt in zip(stale, vectors, texts):
                    h   = embed_hash(txt)
                    uid = e["e.uuid"]
                    await _aset(conn, uid, "graph_embedding",     vec)
                    await _aset(conn, uid, "graph_embed_model",   graph_embed_model_name)
                    await _aset(conn, uid, "graph_embed_content", txt)
                    await _aset(conn, uid, "graph_embed_hash",    h)
                    e["e.graph_embedding"]     = vec
                    e["e.graph_embed_model"]   = graph_embed_model_name
                    e["e.graph_embed_hash"]    = h

            # Build embedding-similarity candidates (stale × all, deduplicated)
            for stale_e in stale:
                emb_a = stale_e.get("e.graph_embedding") or stale_e.get("e.embedding") or []
                if not emb_a:
                    continue
                uid_a = stale_e["e.uuid"]
                for eb in entities:
                    uid_b = eb["e.uuid"]
                    if uid_a == uid_b:
                        continue
                    pair_key = frozenset([uid_a, uid_b])
                    if pair_key in pairs_seen:
                        continue
                    pairs_seen.add(pair_key)
                    emb_b = eb.get("e.graph_embedding") or eb.get("e.embedding") or []
                    if not emb_b:
                        continue
                    score = cosine_similarity(emb_a, emb_b)
                    if score >= threshold:
                        candidates.append((stale_e, eb, score))
        else:
            logger.debug("[memory/librarian] dedup: all embeddings current — name-collision candidates only")

        if not candidates:
            logger.debug("[memory/librarian] dedup: no candidate pairs above threshold %.2f", threshold)
            return

        # Filter already-aliased and cached-distinct pairs
        already_aliased: set[frozenset] = set()
        r = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            "WHERE r.relation = 'ALIASED_TO' AND r.superseded_at IS NULL "
            "RETURN a.uuid, b.uuid"
        )
        while r.has_next():
            row = r.get_next()
            already_aliased.add(frozenset([row[0], row[1]]))

        filtered = [
            (ea, eb, score) for ea, eb, score in candidates
            if frozenset([ea["e.uuid"], eb["e.uuid"]]) not in already_aliased
            and frozenset([ea["e.uuid"], eb["e.uuid"]]) not in distinct_cache
        ]

        if not filtered:
            logger.debug("[memory/librarian] dedup: all candidates already resolved")
            return

        # Partition into pivot-anchored groups, evaluate each with one LLM call.
        # Singleton groups (a node whose only candidate edge was already
        # claimed by an earlier pivot) have nothing to compare against, so
        # skip them rather than spending an LLM call on a guaranteed no-op.
        components = [c for c in _pivot_partition(filtered, batch_size) if len(c) >= 2]
        agent_logger.info(
            "[dedup] %d candidate(s) → %d group(s) (batch_size=%d)",
            len(filtered), len(components), batch_size,
        )
        logger.debug(
            "[memory/librarian] dedup: %d candidate(s) → %d group(s) (batch_size=%d)",
            len(filtered), len(components), batch_size,
        )

        for component in components:
            new_distinct_pairs = await _dedup_group(
                conn, write_lock, llm, component, agent_logger, edges_by_uuid=edges_by_uuid,
            )
            for uid_a, uid_b in new_distinct_pairs:
                _add_to_cache(workspace_path, uid_a, uid_b)
                distinct_cache.add(frozenset([uid_a, uid_b]))

        logger.debug("[memory/librarian] dedup cycle complete")
        agent_logger.info("[dedup] cycle complete")
    except Exception:
        logger.exception("[memory/librarian] dedup cycle error")


# ---------------------------------------------------------------------------
# Dedup: group of N nodes (unified, replaces _dedup_pair + _dedup_batch)
# ---------------------------------------------------------------------------

async def _dedup_group(
    conn,
    write_lock: asyncio.Lock,
    llm,
    entities: list[dict],
    agent_logger: logging.Logger,
    edges_by_uuid: dict[str, list[dict]] | None = None,
) -> list[tuple[str, str]]:
    """
    Ask the LLM to evaluate a group of N entity nodes for deduplication.
    Returns a list of (uid_a, uid_b) pairs that were confirmed distinct,
    so the caller can cache them.

    The LLM returns a list of merge operations (possibly empty). Each operation
    specifies a canonical uuid and one or more duplicate uuids to absorb.
    Silence (empty list) means all nodes in the group are distinct.
    """
    from TinyCTX.ai import TextDelta

    all_uuids = {e["e.uuid"] for e in entities}

    def _fmt_edges(uid: str) -> str:
        edges = (edges_by_uuid or {}).get(uid, [])
        if not edges:
            return "(none)"
        return ", ".join(f"-[{e['relation']}]-> {e['target_name']}" for e in edges)

    node_lines = []
    for e in entities:
        node_lines.append(
            f"  uuid: {e['e.uuid']}\n"
            f"  name: {e['e.name']}\n"
            f"  type: {e['e.entity_type']}\n"
            f"  description: {e['e.description']}\n"
            f"  relationships: {_fmt_edges(e['e.uuid'])}"
        )

    nodes_block = "\n\n".join(f"[{i}]\n{block}" for i, block in enumerate(node_lines))
    prompt = _prompt("dedup_group_user.txt").format(
        node_count=len(entities),
        nodes_block=nodes_block,
    )

    response_text = ""
    async for event in llm.stream(
        [{"role": "system", "content": _prompt("dedup_system.txt")},
         {"role": "user",   "content": prompt}],
        tools=None,
        priority=15,
    ):
        if isinstance(event, TextDelta):
            response_text += event.text

    names_label = "/".join(e["e.name"] for e in entities)
    short_ids   = "/".join(e["e.uuid"][:8] for e in entities)

    try:
        merge_ops = _parse_dedup_response(response_text)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "[memory/librarian] dedup: could not parse group response (%s): %s",
            short_ids, response_text[:200],
        )
        agent_logger.warning("[dedup group/%s] unparseable response: %s", names_label, response_text[:200])
        return []

    if not merge_ops:
        agent_logger.info("[dedup group/%s] no duplicates — all %d distinct", names_label, len(entities))

    # Validate and apply each merge op
    merged_uuids: set[str] = set()
    for op in merge_ops:
        verdict        = op.get("verdict")
        canonical_uuid = op.get("canonical_uuid")
        duplicate_uuids = op.get("duplicate_uuids") or []
        merged_desc    = op.get("merged_description", "")

        if verdict not in ("duplicate", "alias"):
            logger.warning("[memory/librarian] dedup: unknown verdict %r, skipping op", verdict)
            continue

        if not canonical_uuid or canonical_uuid not in all_uuids:
            logger.warning("[memory/librarian] dedup: invalid canonical_uuid %r, skipping op", canonical_uuid)
            continue

        invalid_dups = [u for u in duplicate_uuids if u not in all_uuids or u == canonical_uuid]
        if invalid_dups:
            logger.warning("[memory/librarian] dedup: invalid duplicate_uuids %r, skipping op", invalid_dups)
            continue

        overlap = merged_uuids & (set(duplicate_uuids) | {canonical_uuid})
        if overlap:
            logger.warning("[memory/librarian] dedup: uuids %r appear in multiple ops, skipping op", overlap)
            continue

        merged_uuids.add(canonical_uuid)
        merged_uuids.update(duplicate_uuids)

        for dup_uuid in duplicate_uuids:
            ea = next(e for e in entities if e["e.uuid"] == canonical_uuid)
            eb = next(e for e in entities if e["e.uuid"] == dup_uuid)
            agent_logger.info(
                "[dedup group/%s] %s: '%s' absorbs '%s'",
                names_label, verdict, ea["e.name"], eb["e.name"],
            )
            await _apply_verdict(ea, eb, verdict, canonical_uuid, merged_desc)

    # All pairs NOT involved in a merge op are implicitly distinct
    unmerged = [e["e.uuid"] for e in entities if e["e.uuid"] not in merged_uuids]
    distinct_pairs: list[tuple[str, str]] = []
    for i in range(len(unmerged)):
        for j in range(i + 1, len(unmerged)):
            distinct_pairs.append((unmerged[i], unmerged[j]))

    return distinct_pairs


# ---------------------------------------------------------------------------
# Shared verdict application
# ---------------------------------------------------------------------------

async def _apply_verdict(
    ea: dict,
    eb: dict,
    verdict: str,
    canonical_uuid: str,
    merged_desc: str,
) -> None:
    import TinyCTX.modules.memory.tools as tools
    await tools.kg_merge_entities(
        canonical=canonical_uuid,
        duplicate=eb["e.uuid"] if canonical_uuid == ea["e.uuid"] else ea["e.uuid"],
        merged_description=merged_desc,
        verdict=verdict,
    )

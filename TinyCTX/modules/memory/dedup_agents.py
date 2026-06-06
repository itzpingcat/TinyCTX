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


def _save_distinct_cache(workspace_path: Path, cache: set[frozenset]) -> None:
    """Full replace -- used only as a fallback; prefer _add_to_cache for incremental writes."""
    try:
        con = _open_db(workspace_path)
        con.execute("DELETE FROM distinct_pairs")
        con.executemany(
            "INSERT OR IGNORE INTO distinct_pairs (uuid_a, uuid_b) VALUES (?, ?)",
            [_canonical_pair(*pair) for pair in cache],
        )
        con.commit()
        con.close()
    except Exception:
        logger.exception("[dedup_cache] failed to save cache")


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

def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


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
    logger.info("[memory/librarian] edge dedup starting")
    try:
        # Fetch all active edges grouped by (src_uuid, tgt_uuid, relation).
        r = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            "WHERE r.superseded_at IS NULL "
            "RETURN a.uuid, b.uuid, r.relation, r.created_at"
        )

        from collections import defaultdict
        groups: dict[tuple, list[tuple]] = defaultdict(list)  # key -> [(created_at,)]
        while r.has_next():
            row = r.get_next()
            src, tgt, rel, created_at = row
            groups[(src, tgt, rel)].append((created_at or 0.0,))

        to_delete: list = []  # list of (src, tgt, rel, created_at)
        for (src, tgt, rel), edges in groups.items():
            if len(edges) <= 1:
                continue
            # Sort by created_at descending — keep the newest, delete the rest.
            edges.sort(key=lambda x: x[0], reverse=True)
            to_delete.extend((src, tgt, rel, ca) for (ca,) in edges[1:])

        if not to_delete:
            logger.info("[memory/librarian] edge dedup: no duplicate edges found")
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
                    logger.warning("[memory/librarian] edge dedup: failed to delete edge %s->%s [%s] @ %s: %s", src, tgt, rel, created_at, exc)

        logger.info("[memory/librarian] edge dedup complete")
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
    Refresh graph embeddings for stale entities, then evaluate near-duplicate
    candidate pairs with an LLM and apply merge / alias verdicts.

    Thrash mitigations
    ------------------
    1. Neighbourhood-aware embeddings (name + description + 1-hop edges).
    2. Stale-only comparison: only pairs where >= 1 side was re-embedded.
    3. SQLite distinct-pair cache: confirmed-distinct pairs persist across restarts.
    """
    logger.info("[memory/librarian] dedup cycle starting")
    try:
        from TinyCTX.modules.memory.graph import (
            embed_content_with_edges, embed_hash, cosine_similarity,
        )

        threshold    = float(cfg.get("similarity_threshold", 0.85))
        dedup_batch  = int(cfg.get("dedup_batch_count", 1))
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
            logger.info("[memory/librarian] dedup: fewer than 2 entities, skipping")
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
            logger.info("[memory/librarian] dedup: refreshing %d stale graph embedding(s)", len(stale))
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
            vectors = await embedder.embed(texts)
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
        else:
            logger.info("[memory/librarian] dedup: all embeddings current, no pairs to re-evaluate")
            return

        pairs_seen: set[frozenset] = set()
        candidates: list[tuple[dict, dict, float]] = []

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

        # Name-based candidates: same name (case-insensitive), not already caught by embeddings
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

        if not candidates:
            logger.info("[memory/librarian] dedup: no candidate pairs above threshold %.2f", threshold)
            return

        logger.info("[memory/librarian] dedup: %d candidate pair(s) to evaluate", len(candidates))

        already_aliased: set[frozenset] = set()
        r = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            "WHERE r.relation = 'ALIASED_TO' AND r.superseded_at IS NULL "
            "RETURN a.uuid, b.uuid"
        )
        while r.has_next():
            row = r.get_next()
            already_aliased.add(frozenset([row[0], row[1]]))

        pending = [
            (ea, eb, score) for ea, eb, score in candidates
            if frozenset([ea["e.uuid"], eb["e.uuid"]]) not in already_aliased
            and frozenset([ea["e.uuid"], eb["e.uuid"]]) not in distinct_cache
        ]

        for chunk in _chunks(pending, dedup_batch):
            if dedup_batch == 1:
                ea, eb, _ = chunk[0]
                verdict = await _dedup_pair(conn, write_lock, llm, ea, eb, agent_logger, edges_by_uuid=edges_by_uuid)
                if verdict == "distinct":
                    _add_to_cache(workspace_path, ea["e.uuid"], eb["e.uuid"])
                    distinct_cache.add(frozenset([ea["e.uuid"], eb["e.uuid"]]))
            else:
                pairs = [(ea, eb) for ea, eb, _ in chunk]
                try:
                    results = await _dedup_batch(conn, write_lock, llm, pairs, agent_logger, edges_by_uuid=edges_by_uuid)
                except Exception:
                    logger.warning(
                        "[memory/librarian] dedup: batch of %d failed, retrying individually", len(pairs)
                    )
                    results = []
                    for ea, eb in pairs:
                        verdict = await _dedup_pair(
                            conn, write_lock, llm, ea, eb, agent_logger,
                            edges_by_uuid=edges_by_uuid, cache_on_fail=False
                        )
                        results.append((frozenset([ea["e.uuid"], eb["e.uuid"]]), verdict, False))

                for entry in results:
                    pair_key, verdict, cacheable = entry
                    if verdict == "distinct" and cacheable:
                        distinct_cache.add(pair_key)
                        uids = list(pair_key)
                        _add_to_cache(workspace_path, uids[0], uids[1])

        logger.info("[memory/librarian] dedup cycle complete")
    except Exception:
        logger.exception("[memory/librarian] dedup cycle error")


# ---------------------------------------------------------------------------
# Dedup: single pair
# ---------------------------------------------------------------------------

async def _dedup_pair(
    conn,
    write_lock: asyncio.Lock,
    llm,
    ea: dict,
    eb: dict,
    agent_logger: logging.Logger,
    edges_by_uuid: dict[str, list[dict]] | None = None,
    cache_on_fail: bool = True,
) -> str:
    from TinyCTX.ai import TextDelta

    def _fmt_edges(uid: str) -> str:
        edges = (edges_by_uuid or {}).get(uid, [])
        if not edges:
            return "(none)"
        return ", ".join(f"-[{e['relation']}]-> {e['target_name']}" for e in edges)

    prompt = _prompt("dedup_user.txt").format(
        uuid_a=ea["e.uuid"], name_a=ea["e.name"],
        type_a=ea["e.entity_type"], desc_a=ea["e.description"],
        edges_a=_fmt_edges(ea["e.uuid"]),
        uuid_b=eb["e.uuid"], name_b=eb["e.name"],
        type_b=eb["e.entity_type"], desc_b=eb["e.description"],
        edges_b=_fmt_edges(eb["e.uuid"]),
    )

    response_text = ""
    async for event in llm.stream(
        [{"role": "system", "content": _prompt("dedup_system.txt")},
         {"role": "user",   "content": prompt}],
        tools=None,
    ):
        if isinstance(event, TextDelta):
            response_text += event.text

    if response_text:
        agent_logger.info("[dedup %s/%s] %s", ea["e.uuid"][:8], eb["e.uuid"][:8], response_text)

    try:
        verdicts = _parse_dedup_response(response_text)
        verdict_data = verdicts[0]
    except (json.JSONDecodeError, ValueError, IndexError):
        logger.warning(
            "[memory/librarian] dedup: could not parse verdict for %s/%s: %s",
            ea["e.uuid"][:8], eb["e.uuid"][:8], response_text[:200],
        )
        return "distinct"

    verdict        = verdict_data.get("verdict", "distinct")
    canonical_uuid = verdict_data.get("canonical_uuid")
    merged_desc    = verdict_data.get("merged_description", "")

    if verdict == "distinct":
        return "distinct"

    if not canonical_uuid or canonical_uuid not in {ea["e.uuid"], eb["e.uuid"]}:
        logger.warning("[memory/librarian] dedup: invalid canonical_uuid in verdict")
        return "distinct"

    await _apply_verdict(conn, write_lock, ea, eb, verdict, canonical_uuid, merged_desc)
    return verdict


# ---------------------------------------------------------------------------
# Dedup: batch of pairs
# ---------------------------------------------------------------------------

async def _dedup_batch(
    conn,
    write_lock: asyncio.Lock,
    llm,
    pairs: list[tuple[dict, dict]],
    agent_logger: logging.Logger,
    edges_by_uuid: dict[str, list[dict]] | None = None,
) -> list[tuple[frozenset, str, bool]]:
    from TinyCTX.ai import TextDelta

    def _fmt_edges(uid: str) -> str:
        edges = (edges_by_uuid or {}).get(uid, [])
        if not edges:
            return "(none)"
        return ", ".join(f"-[{e['relation']}]-> {e['target_name']}" for e in edges)

    pair_lines = []
    for idx, (ea, eb) in enumerate(pairs):
        pair_lines.append(
            f"[{idx}]\n"
            f"  Node A: uuid={ea['e.uuid']}  name={ea['e.name']}  "
            f"type={ea['e.entity_type']}  description={ea['e.description']}  "
            f"relationships={_fmt_edges(ea['e.uuid'])}\n"
            f"  Node B: uuid={eb['e.uuid']}  name={eb['e.name']}  "
            f"type={eb['e.entity_type']}  description={eb['e.description']}  "
            f"relationships={_fmt_edges(eb['e.uuid'])}"
        )

    prompt = _prompt("dedup_batch_user.txt").format(
        pairs_block="\n\n".join(pair_lines),
        pair_count=len(pairs),
    )

    response_text = ""
    async for event in llm.stream(
        [{"role": "system", "content": _prompt("dedup_system.txt")},
         {"role": "user",   "content": prompt}],
        tools=None,
    ):
        if isinstance(event, TextDelta):
            response_text += event.text

    if response_text:
        agent_logger.info("[dedup batch/%d] %s", len(pairs), response_text)
    verdicts_data = _parse_dedup_response(response_text)

    if len(verdicts_data) != len(pairs):
        raise ValueError(f"Expected {len(pairs)} verdicts, got {len(verdicts_data)}")

    results: list[tuple[frozenset, str, bool]] = []
    for item in verdicts_data:
        idx            = item.get("pair_index")
        verdict        = item.get("verdict", "distinct")
        canonical_uuid = item.get("canonical_uuid")
        merged_desc    = item.get("merged_description", "")

        if not isinstance(idx, int) or idx < 0 or idx >= len(pairs):
            raise ValueError(f"Invalid pair_index {idx!r} in batch verdict")

        ea, eb = pairs[idx]
        pair_key = frozenset([ea["e.uuid"], eb["e.uuid"]])

        if verdict == "distinct":
            results.append((pair_key, "distinct", True))
            continue

        if not canonical_uuid or canonical_uuid not in {ea["e.uuid"], eb["e.uuid"]}:
            logger.warning(
                "[memory/librarian] dedup batch: invalid canonical_uuid for pair %d, skipping", idx
            )
            results.append((pair_key, "distinct", False))
            continue

        await _apply_verdict(conn, write_lock, ea, eb, verdict, canonical_uuid, merged_desc)
        results.append((pair_key, verdict, True))

    return results


# ---------------------------------------------------------------------------
# Shared verdict application
# ---------------------------------------------------------------------------

async def _apply_verdict(
    conn,
    write_lock: asyncio.Lock,
    ea: dict,
    eb: dict,
    verdict: str,
    canonical_uuid: str,
    merged_desc: str,
) -> None:
    from TinyCTX.modules.memory.graph import now_ts

    dup_uuid = eb["e.uuid"] if canonical_uuid == ea["e.uuid"] else ea["e.uuid"]
    now      = now_ts()

    async with write_lock:
        if verdict == "duplicate":
            logger.info("[memory/librarian] dedup: merging %s -> %s", dup_uuid[:8], canonical_uuid[:8])
            await _aset(conn, canonical_uuid, "description", merged_desc)
            await _aset(conn, canonical_uuid, "updated_at",  now)
            await _aset(conn, canonical_uuid, "embed_hash",  "")
            await conn.execute(
                "MATCH (dup:Entity)-[r:Relation]->(x:Entity), (c:Entity) "
                "WHERE dup.uuid = $dup AND r.superseded_at IS NULL "
                "AND x.uuid <> $canon AND c.uuid = $canon "
                "CREATE (c)-[:Relation {relation: r.relation, weight: r.weight, "
                "description: r.description, created_at: r.created_at, superseded_at: null}]->(x)",
                parameters={"dup": dup_uuid, "canon": canonical_uuid},
            )
            await conn.execute(
                "MATCH (x:Entity)-[r:Relation]->(dup:Entity), (c:Entity) "
                "WHERE dup.uuid = $dup AND r.superseded_at IS NULL "
                "AND x.uuid <> $canon AND c.uuid = $canon "
                "CREATE (x)-[:Relation {relation: r.relation, weight: r.weight, "
                "description: r.description, created_at: r.created_at, superseded_at: null}]->(c)",
                parameters={"dup": dup_uuid, "canon": canonical_uuid},
            )
            await conn.execute(
                "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
                parameters={"uid": dup_uuid},
            )
        elif verdict == "alias":
            logger.info("[memory/librarian] dedup: aliasing %s -> %s", dup_uuid[:8], canonical_uuid[:8])
            await _aset(conn, dup_uuid, "description", merged_desc)
            await _aset(conn, dup_uuid, "updated_at",  now)
            await conn.execute(
                f"MATCH (a:Entity), (c:Entity) "
                f"WHERE a.uuid = $alias AND c.uuid = $canon "
                f"CREATE (a)-[:Relation {{relation: 'ALIASED_TO', weight: 1.0, "
                f"description: 'alias', created_at: {now!r}, superseded_at: null}}]->(c)",
                parameters={"alias": dup_uuid, "canon": canonical_uuid},
            )


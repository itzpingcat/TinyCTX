"""
modules/memory/decay.py

Memory decay sweep for the knowledge librarian.

Computes a 0-1 decay_score for every non-pinned entity from five factors —
priority, distance to nearest pinned entity, active edge count, mention
count, and read/update recency — and hard-deletes entities scoring below
decay_threshold. Pinned entities are never scored and never touched; they
are only used as BFS sources for the distance factor.

Normalisation is min-max, recomputed across the candidate set on every
sweep, so the threshold is relative to the current shape of the graph
rather than a fixed absolute cutoff.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _aset(conn, uid: str, field: str, value):
    return await conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


def _minmax_norm(values: dict[str, float]) -> dict[str, float]:
    """Min-max normalise a uuid->value map to 0-1. Flat input maps to 0.5 for all."""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi == lo:
        return {uid: 0.5 for uid in values}
    span = hi - lo
    return {uid: (v - lo) / span for uid, v in values.items()}


# ---------------------------------------------------------------------------
# Distance to nearest pinned entity — bounded multi-source BFS
# ---------------------------------------------------------------------------

async def _compute_distance_to_pinned(
    conn,
    candidate_uuids: set[str],
    pinned_uuids: set[str],
    max_hops: int,
) -> dict[str, int]:
    """
    Multi-source BFS from all pinned entities at once, edges treated as
    undirected, capped at max_hops. Entities unreached within max_hops (or
    with no path at all) get the sentinel distance max_hops + 1.

    One BFS pass serves every candidate, rather than one BFS per node.
    """
    far = max_hops + 1
    dist: dict[str, int] = {uid: far for uid in candidate_uuids}

    if not pinned_uuids:
        return dist

    r = await conn.execute(
        "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
        "WHERE r.superseded_at IS NULL "
        "RETURN a.uuid, b.uuid"
    )
    adjacency: dict[str, set[str]] = {}
    while r.has_next():
        a, b = r.get_next()
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)  # undirected

    visited: set[str] = set(pinned_uuids)
    frontier: deque[tuple[str, int]] = deque((uid, 0) for uid in pinned_uuids)

    while frontier:
        uid, d = frontier.popleft()
        if d >= max_hops:
            continue
        for neighbor in adjacency.get(uid, ()):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            if neighbor in dist:
                dist[neighbor] = d + 1
            frontier.append((neighbor, d + 1))

    return dist


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

async def compute_decay_scores(
    conn,
    cfg: dict,
) -> dict[str, dict]:
    """
    Compute decay scores for all non-pinned entities.

    Returns {uuid: {"score": float, "factors": {...}, "name": str, "entity_type": str}}.
    Pinned entities are excluded from the result entirely.
    """
    max_hops      = int(cfg.get("decay_max_hops", 4))
    half_life_days = float(cfg.get("decay_half_life_days", 30))

    weights = {
        "priority": float(cfg.get("decay_weight_priority", 0.30)),
        "distance": float(cfg.get("decay_weight_distance", 0.20)),
        "edges":    float(cfg.get("decay_weight_edges",    0.15)),
        "mentions": float(cfg.get("decay_weight_mentions", 0.15)),
        "recency":  float(cfg.get("decay_weight_recency",  0.20)),
    }

    # Pull all entities with the raw fields needed for scoring.
    r = await conn.execute(
        "MATCH (e:Entity) RETURN e.uuid, e.name, e.entity_type, e.pinned_target, "
        "e.priority, e.mention_count, e.updated_at, e.last_read_at"
    )
    rows = []
    while r.has_next():
        rows.append(r.get_next())

    pinned_uuids: set[str] = set()
    candidates: dict[str, dict] = {}
    for uid, name, etype, pinned_target, priority, mention_count, updated_at, last_read_at in rows:
        if pinned_target is not None:
            pinned_uuids.add(uid)
            continue
        candidates[uid] = {
            "name": name,
            "entity_type": etype,
            "priority": float(priority or 0),
            "mention_count": float(mention_count or 0),
            "recency_ts": max(float(updated_at or 0), float(last_read_at or 0)),
        }

    if not candidates:
        return {}

    candidate_uuids = set(candidates.keys())

    # Active edge counts (in + out) for candidates only.
    r = await conn.execute(
        "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
        "WHERE r.superseded_at IS NULL "
        "RETURN a.uuid, b.uuid"
    )
    edge_count: dict[str, int] = {uid: 0 for uid in candidate_uuids}
    while r.has_next():
        a, b = r.get_next()
        if a in edge_count:
            edge_count[a] += 1
        if b in edge_count:
            edge_count[b] += 1

    # Distance to nearest pinned entity.
    distances = await _compute_distance_to_pinned(conn, candidate_uuids, pinned_uuids, max_hops)

    # Raw per-factor values, "higher is safer from decay" orientation.
    raw_priority = {uid: c["priority"] for uid, c in candidates.items()}
    raw_edges    = {uid: float(edge_count.get(uid, 0)) for uid in candidate_uuids}
    raw_mentions = {uid: c["mention_count"] for uid, c in candidates.items()}

    # Distance: invert so closer-to-pinned scores higher.
    raw_inv_distance = {uid: 1.0 / (1.0 + distances.get(uid, max_hops + 1)) for uid in candidate_uuids}

    # Recency: exponential decay on age in days since max(updated_at, last_read_at).
    now = time.time()
    half_life_secs = max(half_life_days, 0.01) * 86400.0
    raw_recency = {}
    for uid, c in candidates.items():
        age_secs = max(now - c["recency_ts"], 0.0)
        raw_recency[uid] = math.exp(-age_secs / half_life_secs * math.log(2))

    norm_priority = _minmax_norm(raw_priority)
    norm_distance = _minmax_norm(raw_inv_distance)
    norm_edges    = _minmax_norm(raw_edges)
    norm_mentions = _minmax_norm(raw_mentions)
    norm_recency  = _minmax_norm(raw_recency)

    results: dict[str, dict] = {}
    for uid, c in candidates.items():
        factors = {
            "priority": norm_priority.get(uid, 0.0),
            "distance": norm_distance.get(uid, 0.0),
            "edges":    norm_edges.get(uid, 0.0),
            "mentions": norm_mentions.get(uid, 0.0),
            "recency":  norm_recency.get(uid, 0.0),
        }
        score = sum(weights[k] * v for k, v in factors.items())
        results[uid] = {
            "score": score,
            "factors": factors,
            "name": c["name"],
            "entity_type": c["entity_type"],
        }

    return results


# ---------------------------------------------------------------------------
# Sweep — score, write back, delete below threshold
# ---------------------------------------------------------------------------

async def run_decay_sweep(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    agent_logger: logging.Logger,
) -> None:
    """
    Score every non-pinned entity and hard-delete those below decay_threshold.

    Writes decay_score back onto every scored entity (including survivors)
    so kg_stats / kg_list / debugdb can surface it. Deletions are logged to
    agent_logger with the full factor breakdown for forensic visibility,
    since the sweep runs fully automatically with no review step.
    """
    logger.debug("[memory/librarian] decay sweep starting")
    threshold = float(cfg.get("decay_threshold", 0.2))

    try:
        scores = await compute_decay_scores(conn, cfg)
    except Exception:
        logger.exception("[memory/librarian] decay sweep: scoring failed")
        return

    if not scores:
        agent_logger.debug("[decay] no non-pinned entities to score")
        return

    to_delete = [uid for uid, r in scores.items() if r["score"] < threshold]

    async with write_lock:
        # Write decay_score onto every scored entity, survivors included.
        for uid, r in scores.items():
            await _aset(conn, uid, "decay_score", r["score"])

        for uid in to_delete:
            r = scores[uid]
            factors = r["factors"]
            agent_logger.info(
                "[decay] deleting [%s] '%s' (uuid=%s) score=%.3f "
                "priority=%.2f distance=%.2f edges=%.2f mentions=%.2f recency=%.2f",
                r["entity_type"], r["name"], uid, r["score"],
                factors["priority"], factors["distance"], factors["edges"],
                factors["mentions"], factors["recency"],
            )
            await conn.execute(
                "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
                parameters={"uid": uid},
            )

    if to_delete:
        logger.info(
            "[memory/librarian] decay sweep: deleted %d entity(ies) below threshold %.2f (scored %d)",
            len(to_delete), threshold, len(scores),
        )
    else:
        agent_logger.debug(
            "[decay] sweep complete — %d entity(ies) scored, none below threshold %.2f",
            len(scores), threshold,
        )

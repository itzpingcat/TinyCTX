"""
False-alias flagger. An ALIASED_TO edge asserts "these are the same thing under
a different name" — but if the two entities' embeddings have low cosine
similarity, the alias is likely a dud (wrong merge target, bad extraction,
stale link after a description rewrite). Flags ALIASED_TO pairs below the
similarity floor for Reviewer follow-up.
"""
from __future__ import annotations

from TinyCTX.modules.memory.graph import cosine_similarity

FLAGGER_TYPE = "false_alias"


def _aliased_pairs(graph_db) -> list[tuple[str, str]]:
    r = graph_db.safe_execute(
        "MATCH (x:Entity)-[r:Relation {relation:'ALIASED_TO'}]->(y:Entity) RETURN x.uuid, y.uuid"
    )
    pairs = []
    while r and r.has_next():
        pairs.append(tuple(r.get_next()))
    return pairs


def _entities_by_uuid(graph_db) -> dict[str, dict]:
    r = graph_db.safe_execute(
        "MATCH (e:Entity) RETURN e.uuid, e.name, e.scope, e.embedding"
    )
    out = {}
    while r and r.has_next():
        uid, name, scope, embedding = r.get_next()
        out[uid] = {"uuid": uid, "name": name, "scope": scope, "embedding": embedding}
    return out


def dud_alias_pairs(pairs: list[tuple[str, str]], entities: dict[str, dict], max_cosine: float) -> list[tuple[dict, dict, float]]:
    """Pure: ALIASED_TO pairs whose cosine < max_cosine. Skips pairs missing an
    entity or an embedding on either side (nothing to score yet)."""
    out = []
    for a_uid, b_uid in pairs:
        a, b = entities.get(a_uid), entities.get(b_uid)
        if not a or not b or not a["embedding"] or not b["embedding"]:
            continue
        score = cosine_similarity(a["embedding"], b["embedding"])
        if score < max_cosine:
            out.append((a, b, score))
    return out


def scan(graph_db, cfg) -> list[dict]:
    max_cosine = float(cfg.get("flaggers", {}).get("false_alias_max_cosine", 0.55))
    pairs = _aliased_pairs(graph_db)
    if not pairs:
        return []
    entities = _entities_by_uuid(graph_db)
    issues = []
    for a, b, score in dud_alias_pairs(pairs, entities, max_cosine):
        issues.append({
            "entity_uuids": sorted([a["uuid"], b["uuid"]]),
            "scope": a["scope"] if a["scope"] == b["scope"] else "global",
            "detail": f"'{a['name']}' ALIASED_TO '{b['name']}' but cosine similarity is only {score:.2f} (< {max_cosine})",
        })
    return issues


def build_prompt(issue) -> str:
    a, b = issue["entity_uuids"]
    return (
        "An ALIASED_TO link looks like a dud: " + issue["detail"] + ". "
        "Read both with search_memory. If they really are the same thing under "
        "a different name, leave the alias (or merge with memory_merge_into). If "
        "they are NOT the same thing, remove the bad alias link and, if they are "
        "genuinely distinct, record that with an IS_NOT relationship "
        "(memory_set_relationship) so this isn't re-flagged.\n\n"
        f"UUIDs: {a}, {b}"
    )

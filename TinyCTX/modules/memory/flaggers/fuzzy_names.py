"""
Fuzzy-name near-duplicate flagger. Complements the embedding deduper by catching
LEXICAL near-duplicates that embeddings miss or mis-score. Uses rapidfuzz when
available, else a difflib fallback. Pairs already linked by IS_NOT are skipped
(the Reviewer writes IS_NOT when it decides two look-alikes are distinct, which
suppresses re-flagging). Cross-scope pairs are allowed; the Reviewer decides how
to reconcile scope.
"""
from __future__ import annotations

from TinyCTX.modules.memory.flaggers._common import all_entities

FLAGGER_TYPE = "fuzzy_names"

try:
    from rapidfuzz import fuzz as _rf  # type: ignore

    def _ratio(a: str, b: str) -> float:
        return _rf.ratio(a, b)
except ImportError:
    import difflib

    def _ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


def similar_name_pairs(entities: list[dict], threshold: float) -> list[tuple[dict, dict, float]]:
    """Pure: all entity pairs whose name similarity >= threshold (0-100)."""
    out = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            a, b = entities[i], entities[j]
            score = _ratio((a["name"] or "").lower(), (b["name"] or "").lower())
            if score >= threshold:
                out.append((a, b, score))
    return out


def _is_not_linked(graph_db, a: str, b: str) -> bool:
    for x, y in ((a, b), (b, a)):
        r = graph_db.safe_execute(
            "MATCH (p:Entity {uuid:$x})-[r:Relation {relation:'IS_NOT'}]->(q:Entity {uuid:$y}) RETURN 1 LIMIT 1",
            {"x": x, "y": y},
        )
        if r and r.has_next():
            return True
    return False


def scan(graph_db, cfg) -> list[dict]:
    threshold = float(cfg.get("fuzzy_name_threshold", 90))
    ents = all_entities(graph_db)
    issues = []
    for a, b, score in similar_name_pairs(ents, threshold):
        if _is_not_linked(graph_db, a["uuid"], b["uuid"]):
            continue
        issues.append({
            "entity_uuids": sorted([a["uuid"], b["uuid"]]),
            "scope": a["scope"] if a["scope"] == b["scope"] else "global",
            "detail": f"'{a['name']}' (scope {a['scope']}) ~ '{b['name']}' (scope {b['scope']}) @ {score:.0f}%",
        })
    return issues


def build_prompt(issue) -> str:
    a, b = issue["entity_uuids"]
    return (
        "Two entity names are very similar: " + issue["detail"] + ". "
        "Read both with search_memory. If they are the SAME thing, merge them with "
        "memory_merge_into (reconcile scope with memory_set_entity_scope if they "
        "differ). If they are genuinely DIFFERENT, record that decision by adding an "
        "IS_NOT relationship (memory_set_relationship) so they are not flagged "
        "again.\n\n"
        f"UUIDs: {a}, {b}"
    )

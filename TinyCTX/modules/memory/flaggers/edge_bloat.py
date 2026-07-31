"""Flag entities whose relationship count is disproportionate to how much their
description actually says about them — a sign the entity picked up junk edges
(duplicates the dedup pass missed, transient mentions, etc.) rather than a sign
it simply has "too many" relationships."""
from __future__ import annotations

from TinyCTX.modules.memory.flaggers._common import all_entities, edge_counts

FLAGGER_TYPE = "edge_bloat"


def scan(graph_db, cfg) -> list[dict]:
    flaggers_cfg = cfg.get("flaggers", {})
    min_edges = int(flaggers_cfg.get("edge_bloat_min_edges", 10))
    chars_per_edge = float(flaggers_cfg.get("edge_bloat_chars_per_edge", 10))
    counts = edge_counts(graph_db)
    issues = []
    for e in all_entities(graph_db):
        edges = counts.get(e["uuid"], 0)
        desc_len = len(e.get("description") or "")
        if edges > min_edges and edges > desc_len / chars_per_edge:
            issues.append({"entity_uuids": [e["uuid"]], "scope": e.get("scope", "global"),
                           "detail": f"{e['name']}:{edges}:{desc_len}"})
    return issues


def build_prompt(issue) -> str:
    name, edges, desc_len = (issue["detail"].split(":", 2) + ["", ""])[:3]
    uid = issue["entity_uuids"][0]
    return (
        f"'{name}' (UUID {uid}) has {edges} relationships against a {desc_len}-char "
        "description — disproportionate, which usually means some of those edges "
        "aren't earning their place. Read the entity and every relationship on it "
        "with search_memory, then check each edge for two specific failure modes:\n"
        "  1. RELATIONS TO DUPLICATE ENTITIES — the edge's target may be a near-"
        "duplicate of another entity already linked here (or of this entity itself) "
        "that the embedding/dedup pass failed to catch. Confirm by comparing names/"
        "descriptions, then merge with memory_merge_into.\n"
        "  2. RELATIONS TO TRANSIENT DATA — the edge's target may be a one-off "
        "mention, a passing event, or something with no lasting relevance to this "
        "entity. Remove just that edge with memory_delete_relationship.\n"
        "Judge every edge individually against these two failure modes — do not "
        "remove or 'clean up' relationships that don't fit either one, even if the "
        "count still looks high afterward."
    )

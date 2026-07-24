"""Shared scan helpers for flaggers."""
from __future__ import annotations


def all_entities(graph_db) -> list[dict]:
    """[{uuid, name, entity_type, description, scope, pinned, mention, updated_at}]."""
    r = graph_db.safe_execute(
        "MATCH (e:Entity) RETURN e.uuid, e.name, e.entity_type, e.description, "
        "e.scope, e.pinned, e.mention, e.updated_at"
    )
    cols = ["uuid", "name", "entity_type", "description", "scope", "pinned", "mention", "updated_at"]
    out = []
    while r and r.has_next():
        out.append(dict(zip(cols, r.get_next())))
    return out


def edge_counts(graph_db) -> dict:
    """uuid -> total incident (in+out) edge count."""
    counts: dict[str, int] = {}
    r = graph_db.safe_execute("MATCH (a:Entity)-[:Relation]->(b:Entity) RETURN a.uuid, b.uuid")
    while r and r.has_next():
        a, b = r.get_next()
        counts[a] = counts.get(a, 0) + 1
        counts[b] = counts.get(b, 0) + 1
    return counts

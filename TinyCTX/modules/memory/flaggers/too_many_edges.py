"""Flag entity pairs with too many relationships between them (even if the
relation types differ) — usually a sign of redundant or over-granular edges."""
from __future__ import annotations

FLAGGER_TYPE = "too_many_edges"


def scan(graph_db, cfg) -> list[dict]:
    limit = int(cfg.get("max_edges_between", 4))
    # count directed edges per ordered pair, then fold to unordered
    r = graph_db.safe_execute(
        "MATCH (a:Entity)-[rel:Relation]->(b:Entity) RETURN a.uuid, b.uuid, a.scope, b.scope"
    )
    pair_count: dict[tuple, int] = {}
    pair_scope: dict[tuple, str] = {}
    while r and r.has_next():
        a, b, sa, sb = r.get_next()
        key = tuple(sorted((a, b)))
        pair_count[key] = pair_count.get(key, 0) + 1
        pair_scope[key] = sa if sa == sb else "global"
    issues = []
    for key, n in pair_count.items():
        if n > limit:
            issues.append({"entity_uuids": list(key), "scope": pair_scope.get(key, "global"),
                           "detail": f"{n} relationships between these two entities"})
    return issues


def build_prompt(issue) -> str:
    a, b = issue["entity_uuids"]
    return (
        f"There are {issue['detail']}. Review the relationships between UUID {a} and "
        f"UUID {b} with search_memory. Consolidate or remove redundant edges using "
        "memory_delete_relationship / memory_set_relationship so only meaningful, "
        "distinct relationships remain."
    )

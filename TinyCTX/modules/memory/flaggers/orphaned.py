"""Flag entities with no relationships at all."""
from __future__ import annotations

from TinyCTX.modules.memory.flaggers._common import all_entities, edge_counts

FLAGGER_TYPE = "orphaned"


def scan(graph_db, cfg) -> list[dict]:
    counts = edge_counts(graph_db)
    issues = []
    for e in all_entities(graph_db):
        if counts.get(e["uuid"], 0) == 0 and not e.get("pinned"):
            issues.append({
                "entity_uuids": [e["uuid"]],
                "scope": e.get("scope", "global"),
                "detail": f"[{e['entity_type']}] {e['name']}: {e['description']}",
            })
    return issues


def build_prompt(issue) -> str:
    return (
        "This entity is orphaned (no relationships). Decide whether it holds "
        "worthwhile information that should be linked to related entities, or "
        "whether it is junk that should be deleted.\n\n"
        f"Entity: {issue['detail']}\n"
        f"UUID: {issue['entity_uuids'][0]}\n\n"
        "Use search_memory to find related entities and memory_set_relationship "
        "to link it, or memory_delete_entity if it has no value."
    )

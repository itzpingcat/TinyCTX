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
        "This entity is orphaned (no relationships). Decide what to do with it:\n\n"
        "1. If it holds worthwhile information connected to the user's context, "
        "find related entities and link it with memory_set_relationship.\n"
        "2. If it is a generic dictionary/encyclopedic definition (a fact you "
        "already know from training data, not something specific to the user "
        "or their data) and it has no relationships, delete it with "
        "memory_delete_entity — it doesn't need to occupy space in the "
        "knowledge graph.\n"
        "3. If it is otherwise junk, delete it with memory_delete_entity.\n\n"
        "Do not keep an entity just because it is 'true' — an orphaned generic "
        "definition is still low-value unless linked to something specific.\n\n"
        f"Entity: {issue['detail']}\n"
        f"UUID: {issue['entity_uuids'][0]}\n\n"
        "Use search_memory to find related entities."
    )

"""Flag entity descriptions that are too long (split) or too short (junk?)."""
from __future__ import annotations

from TinyCTX.modules.memory.flaggers._common import all_entities

FLAGGER_TYPE = "description_length"


def scan(graph_db, cfg) -> list[dict]:
    max_chars = int(cfg.get("desc_max_chars", 1200))
    min_chars = int(cfg.get("desc_min_chars", 15))
    issues = []
    for e in all_entities(graph_db):
        desc = e.get("description") or ""
        if len(desc) > max_chars:
            issues.append({"entity_uuids": [e["uuid"]], "scope": e.get("scope", "global"),
                           "detail": f"too_long:{len(desc)}:{e['name']}"})
        elif len(desc.strip()) < min_chars:
            issues.append({"entity_uuids": [e["uuid"]], "scope": e.get("scope", "global"),
                           "detail": f"too_short:{len(desc.strip())}:{e['name']}"})
    return issues


def build_prompt(issue) -> str:
    kind, length, name = (issue["detail"].split(":", 2) + ["", ""])[:3]
    uid = issue["entity_uuids"][0]
    if kind == "too_long":
        return (
            f"The description of '{name}' (UUID {uid}) is very long ({length} chars). "
            "Read it with search_memory, then shorten it WITHOUT LOSING INFORMATION. "
            "Do NOT delete facts. You may only:\n"
            "  1. MOVE peripheral facts into their own more specific entities "
            "(memory_add_entity for a new entity if needed, then link with "
            "memory_set_relationship), leaving a pointer via the relationship; and/or\n"
            "  2. REMOVE genuinely DUPLICATED data — facts already stated elsewhere in "
            "this same description or already captured on a linked entity.\n"
            "Then rewrite the trimmed description with memory_update_entity_description "
            "(a unified diff). Every fact must still exist somewhere in the graph "
            "afterward — relocating and de-duplicating only, never destroying."
        )
    return (
        f"The description of '{name}' (UUID {uid}) is very short ({length} chars). "
        "Read it with search_memory and decide: enrich it if it names something real, "
        "or delete it with memory_delete_entity if it is junk/noise."
    )

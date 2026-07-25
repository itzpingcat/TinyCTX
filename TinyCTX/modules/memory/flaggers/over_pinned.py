"""Flag scopes with too many pinned entities — the memory block has a token
budget, so over-pinning at a scope crowds it out."""
from __future__ import annotations

from TinyCTX.modules.memory.flaggers._common import all_entities

FLAGGER_TYPE = "over_pinned"


def scan(graph_db, cfg) -> list[dict]:
    limit = int(cfg.get("max_pins_per_scope", 12))
    by_pin: dict[str, list[dict]] = {}
    for e in all_entities(graph_db):
        pin = e.get("pinned")
        if pin:
            by_pin.setdefault(pin, []).append(e)
    issues = []
    for pin, ents in by_pin.items():
        if len(ents) > limit:
            issues.append({
                "entity_uuids": sorted(e["uuid"] for e in ents),
                "scope": pin if pin != "global" else "global",
                "detail": f"{len(ents)} entities pinned at '{pin}' (limit {limit})",
            })
    return issues


def build_prompt(issue) -> str:
    return (
        f"{issue['detail']}. Too many pins dilute the always-on memory block. "
        "Review these pinned entities with search_memory and unpin the least "
        "important ones with memory_set_entity_pinned(name_or_uuid, \"\"), keeping "
        "only the entities that must always be present.\n\n"
        "UUIDs: " + ", ".join(issue["entity_uuids"])
    )

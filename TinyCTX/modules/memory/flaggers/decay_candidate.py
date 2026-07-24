"""
The reborn decay system: instead of an automatic sweep that hard-deleted nodes
(which destroyed quiet-but-important data), this flags quiet, isolated, stale
entities for the Reviewer to *assess*. Nothing is deleted without judgment.

An entity is a decay candidate when it is NOT pinned, has few edges, a low
effective mention (mention decayed by a read-time half-life), and has not been
updated in a long time. Thresholds are absolute (config), never relative to the
current population — so an important-but-quiet node is never mechanically doomed.
"""
from __future__ import annotations

import math
import time

from TinyCTX.modules.memory.flaggers._common import all_entities, edge_counts

FLAGGER_TYPE = "decay_candidate"


def effective_mention(mention: float, updated_at: float, half_life_days: float, now: float | None = None) -> float:
    """mention * 0.5 ** (age_days / half_life). Read-time only; stored mention
    stays monotonic."""
    now = now if now is not None else time.time()
    if not updated_at:
        return float(mention or 0.0)
    age_days = max(0.0, (now - updated_at) / 86400.0)
    return float(mention or 0.0) * math.pow(0.5, age_days / max(half_life_days, 0.001))


def scan(graph_db, cfg) -> list[dict]:
    half_life = float(cfg.get("mention_half_life_days", 30))
    min_eff = float(cfg.get("decay_min_effective_mention", 0.5))
    max_edges = int(cfg.get("decay_max_edges", 1))
    stale_days = float(cfg.get("decay_stale_days", 90))
    now = time.time()
    counts = edge_counts(graph_db)
    issues = []
    for e in all_entities(graph_db):
        if e.get("pinned"):
            continue
        eff = effective_mention(e.get("mention") or 0.0, e.get("updated_at") or 0.0, half_life, now)
        edges = counts.get(e["uuid"], 0)
        age_days = (now - (e.get("updated_at") or now)) / 86400.0
        if eff < min_eff and edges <= max_edges and age_days >= stale_days:
            issues.append({
                "entity_uuids": [e["uuid"]],
                "scope": e.get("scope", "global"),
                "detail": f"{e['name']} (eff_mention={eff:.2f}, edges={edges}, age={age_days:.0f}d)",
            })
    return issues


def build_prompt(issue) -> str:
    return (
        "This entity looks stale, quiet and isolated: " + issue["detail"] + ". "
        "It is a DECAY CANDIDATE, not a deletion order. Read it with search_memory "
        "and decide: link it to relevant entities if it still matters, leave it "
        "alone if it is worth keeping, or delete it with memory_delete_entity only "
        "if it is genuinely worthless. Do NOT delete merely because it is quiet.\n\n"
        f"UUID: {issue['entity_uuids'][0]}"
    )

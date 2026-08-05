"""
modules/memory/format.py

Shared entity-to-text formatting, used by both search_memory (tools.py) and
the <memory> injection block (__main__.py). One formatter, three detail
levels, so the two call sites can't drift out of sync.

Detail levels
-------------
low (default):
    [Type] name
    description
    >
    REL_A: target1, target2
    REL_B: target3
    <
    source1: REL_C
    source2: REL_D
  Header and description are each their own line, immediately followed (no
  blank line) by a ">" marker line and one line per outgoing relation group
  ("REL: t1, t2"), then (only if incoming edges exist) a "<" marker line and
  one line per incoming edge ("source: REL"). The ">" section is omitted if
  there are no outgoing edges; likewise "<" if there are no incoming edges.
  Relations listed in prompts/noisy_relationships.txt are dropped entirely.
  Description truncated at desc_truncate_chars (relationships never are).
  A relation that exists in both directions between the same pair (A -[REL]->
  B and B -[REL]-> A) is one fact, not two: it's shown once, on the outgoing
  side, tagged "(mutual)", and dropped from the incoming list.

medium:
  low, plus pinned/scope shown in the header, and noisy relations included.

high:
  medium, plus uuid, created_at, updated_at, and per-edge weights.
"""
from __future__ import annotations

from pathlib import Path

_NOISY_RELATIONS: set[str] | None = None


def _noisy_relations_file() -> Path:
    return Path(__file__).parent / "prompts" / "noisy_relationships.txt"


def _load_noisy_relations() -> set[str]:
    """Lazy-loaded, cached. One relation per line; '#' comments and blanks skipped."""
    global _NOISY_RELATIONS
    if _NOISY_RELATIONS is not None:
        return _NOISY_RELATIONS
    relations: set[str] = set()
    try:
        lines = _noisy_relations_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        relations.add(line.upper())
    _NOISY_RELATIONS = relations
    return relations


def _group_by_relation(edges: list[dict], name_key: str) -> list[tuple[str, list[dict]]]:
    """Group edges by relation, preserving first-seen relation order."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for edge in edges:
        rel = edge.get("relation", "")
        if rel not in groups:
            groups[rel] = []
            order.append(rel)
        groups[rel].append(edge)
    return [(rel, groups[rel]) for rel in order]


def _split_mutual(edges_out: list[dict], edges_in: list[dict]) -> tuple[set, list[dict]]:
    """Same relation, same pair, both directions (A -[REL]-> B and B -[REL]-> A) is
    one fact, not two — flag it and drop the incoming half so it isn't printed
    twice as if it were two independent relationships."""
    out_keys = {(e.get("relation"), e.get("target_uuid")) for e in edges_out}
    mutual: set = set()
    remaining_in = []
    for e in edges_in:
        key = (e.get("relation"), e.get("source_uuid"))
        if key in out_keys:
            mutual.add(key)
        else:
            remaining_in.append(e)
    return mutual, remaining_in


def format_entity(e: dict, *, detail: str = "low", desc_truncate_chars: int = 2500) -> str:
    """Render one entity dict (as returned by GraphDB.get_entity) as text."""
    if detail not in ("low", "medium", "high"):
        detail = "low"

    name = e.get("e.name", "?")
    et = e.get("e.entity_type", "?")
    desc = e.get("e.description", "") or ""
    pin = e.get("e.pinned", "")
    scope = e.get("e.scope", "")
    uid = e.get("e.uuid", "?")

    if detail == "low" and desc_truncate_chars and len(desc) > desc_truncate_chars:
        desc = desc[:desc_truncate_chars].rstrip() + "…"

    header = f"[{et}] {name}"
    if detail in ("medium", "high"):
        tags = []
        if pin:
            tags.append("pinned")
        if scope:
            tags.append(f"scope={scope}")
        if detail == "high":
            tags.append(f"UUID: {uid}")
        if tags:
            header += f" ({', '.join(tags)})"
    lines = [header]
    if desc:
        lines.append(desc)

    edges_out = list(e.get("edges_out", []))
    edges_in = list(e.get("edges_in", []))
    if detail == "low":
        noisy = _load_noisy_relations()
        edges_out = [x for x in edges_out if x.get("relation") not in noisy]
        edges_in = [x for x in edges_in if x.get("relation") not in noisy]

    mutual, edges_in = _split_mutual(edges_out, edges_in)

    def _fmt_target(edge: dict, key: str) -> str:
        base = edge.get(key, "?")
        if (edge.get("relation"), edge.get("target_uuid")) in mutual:
            base += " (mutual)"
        if detail == "high":
            base += f"(w={edge.get('weight')})"
        return base

    out_lines = []
    for rel, group in _group_by_relation(edges_out, "target_name"):
        targets = ", ".join(_fmt_target(x, "target_name") for x in group)
        out_lines.append(f"{rel}: {targets}")

    in_lines = []
    for edge in edges_in:
        src = edge.get("source_name", "?")
        if detail == "high":
            src += f"(w={edge.get('weight')})"
        in_lines.append(f"{src}: {edge.get('relation', '?')}")

    if out_lines:
        lines.append(">")
        lines.extend(out_lines)
    if in_lines:
        lines.append("<")
        lines.extend(in_lines)

    if detail == "high":
        created = e.get("e.created_at")
        updated = e.get("e.updated_at")
        if created is not None or updated is not None:
            lines.append(f"created_at={created} updated_at={updated}")

    return "\n".join(lines)


def format_entities(entities: list[dict], *, detail: str = "low",
                     desc_truncate_chars: int = 2500, exact_uuid: str | None = None) -> str:
    """Render a list of entities, blank-line separated. Marks the exact match if given."""
    blocks = []
    for e in entities:
        if not e:
            continue
        block = format_entity(e, detail=detail, desc_truncate_chars=desc_truncate_chars)
        if exact_uuid and e.get("e.uuid") == exact_uuid:
            first, _, rest = block.partition("\n")
            block = first + "  [exact]" + (("\n" + rest) if rest else "")
        blocks.append(block)
    return "\n\n".join(blocks).strip() or "No matching entities found."

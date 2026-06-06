"""
modules/memory/tools.py

Memory tool functions for the knowledge graph.
Call init(conn, write_lock, graph_db, embedder) before using any tools.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from TinyCTX.modules.memory.graph import new_uuid, now_ts, top_k_cosine

logger = logging.getLogger(__name__)

_conn:           Any = None
_write_lock:     Any = None
_graph_db:       Any = None
_embedder:       Any = None
_query_template: str = "{text}"
_doc_template:   str = "{text}"


def init(
    conn,
    write_lock: asyncio.Lock,
    graph_db,
    embedder,
    *,
    query_template: str = "{text}",
    doc_template: str = "{text}",
):
    global _conn, _write_lock, _graph_db, _embedder, _query_template, _doc_template
    _conn             = conn
    _write_lock       = write_lock
    _graph_db         = graph_db
    _embedder         = embedder
    _query_template   = query_template
    _doc_template     = doc_template


# ---------------------------------------------------------------------------
# Shared async helper
# ---------------------------------------------------------------------------

async def _aset(uid: str, field: str, value):
    return await _conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def kg_add_entity(
    name: str,
    entity_type: str,
    description: str,
    priority: int = 40,
    pinned: str = "",
    pinned_target: str = "",
) -> str:
    """
    Add a new knowledge graph entity. Returns an error if an entity with the
    same name and type already exists — use kg_update_entity to modify it.

    Args:
        name: Display name of the entity.
        entity_type: One of: Person, Concept, Preference, Fact, Event,
            Location, Organization, Project, Technology, Rule, Directive, Role.
        description: 1-3 sentence factual description.
        priority: 0-100 importance score (default 40).
        pinned: Pin scope — "global" (always inject into system prompt) or
            "user" (inject only when pinned_target user is active). Leave empty
            to not pin.
        pinned_target: TinyCTX username of the user to target. Only used when
            pinned="user".
    """
    now = now_ts()
    r = await _conn.execute(
        "MATCH (e:Entity) WHERE e.name = $name AND e.entity_type = $et RETURN e.uuid LIMIT 1",
        parameters={"name": name, "et": entity_type},
    )
    if r.has_next():
        uid = r.get_next()[0]
        # Return the entity's current state so the agent can decide whether to
        # call kg_update_entity without a redundant kg_get_entity round-trip.
        existing = _graph_db.get_entity(uid)
        if existing:
            ex_desc   = existing.get("e.description", "")
            ex_pri    = existing.get("e.priority", "?")
            ex_pin    = existing.get("e.pinned_target")
            pin_note  = f"  [pinned:{ex_pin}]" if ex_pin else ""
            lines     = [
                f"Error: {entity_type} '{name}' already exists (UUID: {uid}).{pin_note}",
                f"  Description: {ex_desc}",
                f"  Priority: {ex_pri}",
            ]
            for edge in existing.get("edges_out", []):
                w    = edge.get("weight", "")
                note = f" — {edge['description']}" if edge.get("description") else ""
                lines.append(f"  ->[{edge['relation']}]-> {edge['target_name']} (UUID: {edge['target_uuid']}) (w={w}){note}")
            for edge in existing.get("edges_in", []):
                w    = edge.get("weight", "")
                note = f" — {edge['description']}" if edge.get("description") else ""
                lines.append(f"  <-[{edge['relation']}]<- {edge['source_name']} (UUID: {edge['source_uuid']}) (w={w}){note}")
            lines.append("Use kg_update_entity to modify it.")
            return "\n".join(lines)
        return (
            f"Error: {entity_type} '{name}' already exists (UUID: {uid}). "
            f"Use kg_update_entity to modify it."
        )

    uid = new_uuid()
    # Resolve stored pinned_target value
    stored_pin = None
    if pinned == "global":
        stored_pin = "global"
    elif pinned == "user" and pinned_target.strip():
        stored_pin = pinned_target.strip()
    async with _write_lock:
        await _conn.execute("CREATE (e:Entity {uuid: $uid})", parameters={"uid": uid})
        await _aset(uid, "name",          name)
        await _aset(uid, "entity_type",   entity_type)
        await _aset(uid, "description",   description)
        await _aset(uid, "pinned_target", stored_pin)
        await _aset(uid, "priority",      priority)
        await _aset(uid, "mention_count", 0)
        await _aset(uid, "created_at",    now)
        await _aset(uid, "updated_at",    now)
        await _aset(uid, "embed_model",   "")
        await _aset(uid, "embed_content", "")
        await _aset(uid, "embed_hash",    "")
    pin_note = f"  [pinned:{stored_pin}]" if stored_pin else ""
    return (
        f"Added {entity_type} '{name}' (UUID: {uid}){pin_note}\n"
        f"  Description: {description}\n"
        f"  Priority: {priority}"
    )


async def kg_update_entity(
    uuid: str,
    description: str | None = None,
    priority: int | None = None,
    pinned: str | None = None,
    pinned_target: str = "",
) -> str:
    """
    Update fields on an existing entity. Only provided fields are changed.

    Args:
        uuid: The entity UUID.
        description: New description (optional).
        priority: New priority value (optional).
        pinned: Pin scope — "global", "user", or "" to clear pinning. Pass
            None to leave pinning unchanged.
        pinned_target: TinyCTX username to target. Only used when pinned="user".
    """
    if description is None and priority is None and pinned is None:
        return f"No fields to update — nothing changed for UUID {uuid}."

    entity = _graph_db.get_entity(uuid)
    if not entity:
        return f"Entity UUID {uuid} not found — nothing updated."

    name     = entity.get("e.name", uuid)
    etype    = entity.get("e.entity_type", "Entity")
    old_desc = entity.get("e.description", "") or ""
    old_pri  = entity.get("e.priority")
    old_pin  = entity.get("e.pinned_target")

    now = now_ts()
    async with _write_lock:
        if description is not None:
            await _aset(uuid, "description", description)
            await _aset(uuid, "embed_hash",  "")
        if priority is not None:
            await _aset(uuid, "priority", priority)
        if pinned is not None:
            if pinned == "global":
                new_pin = "global"
            elif pinned == "user" and pinned_target.strip():
                new_pin = pinned_target.strip()
            else:  # "" or anything else clears pinning
                new_pin = None
            await _aset(uuid, "pinned_target", new_pin)
        await _aset(uuid, "updated_at", now)

    lines = [f"Updated {etype} '{name}' (UUID: {uuid})"]
    if description is not None:
        if old_desc.strip() != description.strip():
            lines.append(f"  Description was: {old_desc}")
            lines.append(f"  Description now: {description}")
        else:
            lines.append("  Description: unchanged")
    if priority is not None:
        lines.append(f"  Priority: {old_pri} → {priority}")
    if pinned is not None:
        new_pin_display = new_pin or "(not pinned)"
        old_pin_display = old_pin or "(not pinned)"
        lines.append(f"  Pinned: {old_pin_display} → {new_pin_display}")
    return "\n".join(lines)


async def kg_add_relationship(
    source_uuid: str,
    target_uuid: str,
    relation: str,
    weight: float = 0.5,
    description: str = "",
) -> str:
    """
    Add a directed relationship between two entities.
    No-op if an active edge with the same relation already exists between
    these two entities — returns the existing edge instead.

    Args:
        source_uuid: UUID of the source entity.
        target_uuid: UUID of the target entity.
        relation: UPPER_SNAKE_CASE relation label.
        weight: Strength 0.0-1.0 (default 0.5).
        description: Optional explanation.
    """
    src_entity = _graph_db.get_entity_slim(source_uuid)
    tgt_entity = _graph_db.get_entity_slim(target_uuid)
    src_name   = src_entity["name"] if src_entity else source_uuid
    tgt_name   = tgt_entity["name"] if tgt_entity else target_uuid

    rel = relation.upper().replace("'", "")

    # Check for existing active edge with the same relation type.
    existing = await _conn.execute(
        f"MATCH (a:Entity)-[r:Relation]->(b:Entity) "
        f"WHERE a.uuid = $src AND b.uuid = $tgt "
        f"AND r.relation = '{rel}' AND r.superseded_at IS NULL "
        f"RETURN r.weight, r.description LIMIT 1",
        parameters={"src": source_uuid, "tgt": target_uuid},
    )
    if existing.has_next():
        row = existing.get_next()
        ex_w, ex_desc = row[0], row[1]
        ex_note = f" — {ex_desc}" if ex_desc else ""
        return (
            f"Relationship already exists: '{src_name}' -[{rel}]-> '{tgt_name}' "
            f"(w={ex_w}){ex_note}. Use kg_delete_relationship then kg_add_relationship to replace it."
        )

    now  = now_ts()
    desc = description.replace("'", "''")
    async with _write_lock:
        await _conn.execute(
            f"MATCH (a:Entity), (b:Entity) WHERE a.uuid = $src AND b.uuid = $tgt "
            f"CREATE (a)-[:Relation {{relation: '{rel}', weight: {weight!r}, "
            f"description: '{desc}', created_at: {now!r}, superseded_at: null}}]->(b)",
            parameters={"src": source_uuid, "tgt": target_uuid},
        )
    desc_note = f"\n  Note: {description}" if description else ""
    return (
        f"Added relationship: '{src_name}' -[{rel}]-> '{tgt_name}' (weight: {weight}){desc_note}"
    )


async def kg_delete_entity(uuid: str) -> str:
    """
    Hard-delete an entity and all its edges. Use sparingly.

    Args:
        uuid: The entity UUID to delete.
    """
    entity = _graph_db.get_entity_slim(uuid)
    name   = entity["name"] if entity else uuid
    etype  = entity["entity_type"] if entity else "Entity"

    async with _write_lock:
        await _conn.execute(
            "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
            parameters={"uid": uuid},
        )
    return f"Deleted {etype} '{name}' (UUID: {uuid}) and all its relationships."


async def kg_delete_relationship(src_uuid: str, tgt_uuid: str, relation: str) -> str:
    """
    Delete all active edges of a given relation type between two entities.

    Args:
        src_uuid: Source entity UUID.
        tgt_uuid: Target entity UUID.
        relation: The relation label to delete.
    """
    src_entity = _graph_db.get_entity_slim(src_uuid)
    tgt_entity = _graph_db.get_entity_slim(tgt_uuid)
    src_name   = src_entity["name"] if src_entity else src_uuid
    tgt_name   = tgt_entity["name"] if tgt_entity else tgt_uuid

    rel = relation.upper().replace("'", "")
    async with _write_lock:
        await _conn.execute(
            f"MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            f"WHERE a.uuid = $src AND b.uuid = $tgt "
            f"AND r.relation = '{rel}' AND r.superseded_at IS NULL DELETE r",
            parameters={"src": src_uuid, "tgt": tgt_uuid},
        )
    return f"Deleted relationship: '{src_name}' -[{rel}]-> '{tgt_name}'."


async def kg_search(query: str, top_k: int = 5, semantic: bool = True) -> str:
    """
    Search the knowledge graph for entities relevant to a query.
    Returns matching entities with their direct active relationships.
    Bumps mention_count on returned nodes.

    Use this to check whether an entity already exists before calling
    kg_add_entity.

    Args:
        query: Natural language query or keywords to search for.
        top_k: Maximum number of entities to return (default 5).
        semantic: If true (default), use vector similarity search.
            If false or no embedding model configured, uses keyword search.
    """
    # Check for an exact name match — pin it to the top but continue searching.
    exact_matches = _graph_db.find_entity(name=query)
    exact = next((e for e in exact_matches if e["name"].lower() == query.lower()), None)
    exact_uid = exact["uuid"] if exact else None

    query_vec = None
    if semantic and _embedder is not None:
        try:
            query_vec = await _embedder.embed_one(_query_template.format(text=query))
        except Exception as exc:
            logger.warning("[memory] kg_search embed failed: %s -- falling back to keyword", exc)

    if query_vec is not None:
        all_embs = _graph_db.all_entities_with_embeddings()
        uids     = [uid for uid, _ in top_k_cosine(query_vec, all_embs, top_k)]
    else:
        uids = [r["uuid"] for r in _graph_db.find_entity(name=query)[:top_k]]

    # Prepend exact match (if any) and deduplicate, preserving order.
    if exact_uid:
        uids = [exact_uid] + [u for u in uids if u != exact_uid]

    if not uids:
        return "No matching entities found."

    _graph_db.bump_mention_count(uids)

    lines = []
    for uid in uids:
        entity = _graph_db.get_entity(uid)
        if not entity:
            continue
        name  = entity.get("e.name", "?")
        etype = entity.get("e.entity_type", "?")
        desc  = entity.get("e.description", "")
        pri   = entity.get("e.priority", "?")
        pin   = entity.get("e.pinned_target")
        pin_note = f"  [pinned:{pin}]" if pin else ""
        exact_note = "  [exact match]" if uid == exact_uid else ""
        lines.append(f"[{etype}] {name} (UUID: {uid}){pin_note}  priority: {pri}{exact_note}")
        if desc:
            lines.append(f"  {desc}")
        for edge in entity.get("edges_out", []):
            w    = edge.get("weight", "")
            note = f" — {edge['description']}" if edge.get("description") else ""
            lines.append(f"  ->[{edge['relation']}]-> {edge['target_name']} (UUID: {edge['target_uuid']}) (w={w}){note}")
        for edge in entity.get("edges_in", []):
            w    = edge.get("weight", "")
            note = f" — {edge['description']}" if edge.get("description") else ""
            lines.append(f"  <-[{edge['relation']}]<- {edge['source_name']} (UUID: {edge['source_uuid']}) (w={w}){note}")
        lines.append("")

    return "\n".join(lines).strip() if lines else "No entities found."


async def kg_traverse(uuid: str, hops: int = 1, relation_filter: str = "") -> str:
    """
    Walk the graph from an entity outward up to N hops.
    Returns all active edges encountered.

    Args:
        uuid: Starting entity UUID.
        hops: Number of hops to traverse (default 1, max 3).
        relation_filter: If provided, only follow edges with this relation label.
    """
    hops       = min(int(hops), 3)
    start      = _graph_db.get_entity_slim(uuid)
    start_name = start["name"] if start else uuid

    edges = _graph_db.traverse(uuid, hops, relation_filter or None)
    if not edges:
        filter_note = f" with relation [{relation_filter.upper()}]" if relation_filter else ""
        return f"No edges found from '{start_name}' (UUID: {uuid}){filter_note}."

    filter_note = f" (filtered to [{relation_filter.upper()}])" if relation_filter else ""
    lines = [f"Traversal from '{start_name}' (UUID: {uuid}), {hops} hop(s){filter_note}:"]
    for e in edges:
        lines.append(
            f"  '{e.get('source_name', start_name)}' ->[{e['relation']}]-> '{e['target_name']}' (UUID: {e['target_uuid']})"
        )
    return "\n".join(lines)


def _format_entity(uuid: str, entity: dict) -> str:
    """Shared formatter for kg_get_entity output."""
    name  = entity.get("e.name", "?")
    etype = entity.get("e.entity_type", "?")
    desc  = entity.get("e.description", "")
    pin   = entity.get("e.pinned_target")
    pri   = entity.get("e.priority", "?")
    mens  = entity.get("e.mention_count", 0)
    pin_note = f"[pinned:{pin}]" if pin else ""
    lines = [
        f"[{etype}] {name}",
        f"  UUID:        {uuid}",
        f"  Pinned:      {pin_note or '(not pinned)'}  |  Priority: {pri}  |  Mentions: {mens}",
        f"  Description: {desc}",
    ]
    out_edges = entity.get("edges_out", [])
    in_edges  = entity.get("edges_in",  [])
    if out_edges:
        lines.append("  Outgoing relationships:")
        for e in out_edges:
            note = f" — {e['description']}" if e.get("description") else ""
            lines.append(f"    ->[{e['relation']}]-> '{e['target_name']}' (UUID: {e['target_uuid']}){note}")
    if in_edges:
        lines.append("  Incoming relationships:")
        for e in in_edges:
            note = f" — {e['description']}" if e.get("description") else ""
            lines.append(f"    <-[{e['relation']}]<- '{e['source_name']}' (UUID: {e['source_uuid']}){note}")
    if not out_edges and not in_edges:
        lines.append("  No relationships.")
    return "\n".join(lines)


async def kg_get_entity(uuid_or_name: str) -> str:
    """
    Retrieve full details of a knowledge graph entity including all
    active incoming and outgoing relationships.

    Args:
        uuid_or_name: The entity UUID or exact entity name to retrieve.
    """
    # Try UUID lookup first.
    entity = _graph_db.get_entity(uuid_or_name)
    if entity:
        return _format_entity(uuid_or_name, entity)

    # Fall back to name lookup.
    matches = _graph_db.find_entity(name=uuid_or_name)
    exact   = next((e for e in matches if e["name"].lower() == uuid_or_name.lower()), None)
    if exact:
        entity = _graph_db.get_entity(exact["uuid"])
        if entity:
            return _format_entity(exact["uuid"], entity)

    # Ambiguous partial matches — list them so the caller can retry with a UUID.
    if matches:
        lines = [f"No exact match for '{uuid_or_name}'. Did you mean:"]
        for m in matches[:5]:
            lines.append(f"  [{m['entity_type']}] {m['name']} (UUID: {m['uuid']})")
        return "\n".join(lines)

    return f"Entity '{uuid_or_name}' not found."




async def kg_list(entity_type: str = "", pinned_only: bool = False) -> str:
    """
    List knowledge graph entities, optionally filtered by type or pinned status.

    Args:
        entity_type: Filter by type (e.g. Person, Project, Technology). Empty = all types.
        pinned_only: If true, return only pinned entities.
    """
    entities = _graph_db.list_entities(entity_type=entity_type or None, pinned_only=pinned_only)
    if not entities:
        filter_note = ""
        if entity_type and pinned_only:
            filter_note = f" matching type '{entity_type}' that are pinned"
        elif entity_type:
            filter_note = f" of type '{entity_type}'"
        elif pinned_only:
            filter_note = " that are pinned"
        return f"No entities found{filter_note}."

    lines = []
    for e in entities:
        pin = f"  [pinned:{e.get('pinned_target')}]" if e.get("pinned_target") else ""
        lines.append(
            f"[{e['entity_type']}] {e['name']} (UUID: {e['uuid']}){pin}  priority: {e['priority']}\n"
            f"  {e['description']}"
        )
    return "\n\n".join(lines)


async def kg_stats() -> str:
    """
    Show knowledge graph statistics: entity count, edge count, pinned entities,
    priority distribution, embedding coverage, and most-mentioned entities.
    """
    s = _graph_db.get_stats()

    embedded_note = (
        f"{s['embedded_count']} of {s['entity_count']} entities have embeddings"
        if s["entity_count"] > 0 else "no entities"
    )

    lines = [
        f"Knowledge graph: {s['entity_count']} entities, {s['active_edge_count']} active relationships"
        + (f", {s['superseded_edge_count']} superseded" if s["superseded_edge_count"] else ""),
        f"Pinned entities: {s['pinned_count']}",
        f"Average priority: {s['avg_priority']}",
        f"Embedding coverage: {embedded_note}",
        "",
        "Entities by type:",
    ]
    for etype, count in s["by_type"].items():
        lines.append(f"  {etype}: {count}")

    if s["top_mentioned"]:
        lines.append("")
        lines.append("Most mentioned:")
        for e in s["top_mentioned"]:
            lines.append(f"  {e['name']} ({e['entity_type']}): {e['mention_count']} mentions")

    return "\n".join(lines)

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

_conn:       Any = None
_write_lock: Any = None
_graph_db:   Any = None
_embedder:   Any = None


def init(conn, write_lock: asyncio.Lock, graph_db, embedder):
    global _conn, _write_lock, _graph_db, _embedder
    _conn       = conn
    _write_lock = write_lock
    _graph_db   = graph_db
    _embedder   = embedder


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
    pinned: bool = False,
) -> str:
    """
    Add or update a knowledge graph entity. Returns the entity UUID.
    Uses MERGE on name+type so duplicate calls are idempotent.

    Args:
        name: Display name of the entity.
        entity_type: One of: Person, Concept, Preference, Fact, Event,
            Location, Organization, Project, Technology, Rule, Directive, Role.
        description: 1-3 sentence factual description.
        priority: 0-100 importance score (default 40).
        pinned: If true, inject into every system prompt.
    """
    now = now_ts()
    r = await _conn.execute(
        "MATCH (e:Entity) WHERE e.name = $name AND e.entity_type = $et RETURN e.uuid LIMIT 1",
        parameters={"name": name, "et": entity_type},
    )
    if r.has_next():
        uid = r.get_next()[0]
        async with _write_lock:
            await _aset(uid, "description", description)
            await _aset(uid, "updated_at",  now)
            await _aset(uid, "priority",    priority)
            await _aset(uid, "pinned",      pinned)
            await _aset(uid, "embed_hash",  "")
        return uid
    uid = new_uuid()
    async with _write_lock:
        await _conn.execute("CREATE (e:Entity {uuid: $uid})", parameters={"uid": uid})
        await _aset(uid, "name",          name)
        await _aset(uid, "entity_type",   entity_type)
        await _aset(uid, "description",   description)
        await _aset(uid, "pinned",        pinned)
        await _aset(uid, "priority",      priority)
        await _aset(uid, "mention_count", 0)
        await _aset(uid, "created_at",    now)
        await _aset(uid, "updated_at",    now)
        await _aset(uid, "embed_model",   "")
        await _aset(uid, "embed_content", "")
        await _aset(uid, "embed_hash",    "")
    return uid


async def kg_update_entity(
    uuid: str,
    description: str | None = None,
    priority: int | None = None,
    pinned: bool | None = None,
) -> str:
    """
    Update fields on an existing entity. Only provided fields are changed.

    Args:
        uuid: The entity UUID.
        description: New description (optional).
        priority: New priority value (optional).
        pinned: New pinned flag (optional).
    """
    now = now_ts()
    if description is None and priority is None and pinned is None:
        return f"[no fields to update for {uuid}]"
    async with _write_lock:
        if description is not None:
            await _aset(uuid, "description", description)
            await _aset(uuid, "embed_hash",  "")
        if priority is not None:
            await _aset(uuid, "priority", priority)
        if pinned is not None:
            await _aset(uuid, "pinned", pinned)
        await _aset(uuid, "updated_at", now)
    return f"updated {uuid}"


async def kg_add_relationship(
    source_uuid: str,
    target_uuid: str,
    relation: str,
    weight: float = 0.5,
    description: str = "",
) -> str:
    """
    Add a directed relationship between two entities.

    Args:
        source_uuid: UUID of the source entity.
        target_uuid: UUID of the target entity.
        relation: UPPER_SNAKE_CASE relation label.
        weight: Strength 0.0-1.0 (default 0.5).
        description: Optional explanation.
    """
    now  = now_ts()
    rel  = relation.upper().replace("'", "")
    desc = description.replace("'", "''")
    async with _write_lock:
        await _conn.execute(
            f"MATCH (a:Entity), (b:Entity) WHERE a.uuid = $src AND b.uuid = $tgt "
            f"CREATE (a)-[:Relation {{relation: '{rel}', weight: {weight!r}, "
            f"description: '{desc}', created_at: {now!r}, superseded_at: null}}]->(b)",
            parameters={"src": source_uuid, "tgt": target_uuid},
        )
    return f"added {relation} from {source_uuid[:8]} -> {target_uuid[:8]}"


async def kg_supersede_relationship(
    src_uuid: str,
    tgt_uuid: str,
    old_relation: str,
    new_relation: str,
    weight: float = 0.5,
    description: str = "",
) -> str:
    """
    Mark an existing relationship as superseded and create a replacement.

    Args:
        src_uuid: Source entity UUID.
        tgt_uuid: Target entity UUID.
        old_relation: The relation label to supersede.
        new_relation: The new relation label to create.
        weight: Weight for the new relationship.
        description: Optional explanation.
    """
    now  = now_ts()
    old  = old_relation.upper().replace("'", "")
    new  = new_relation.upper().replace("'", "")
    desc = description.replace("'", "''")
    async with _write_lock:
        await _conn.execute(
            f"MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            f"WHERE a.uuid = $src AND b.uuid = $tgt "
            f"AND r.relation = '{old}' AND r.superseded_at IS NULL "
            f"SET r.superseded_at = {now!r}",
            parameters={"src": src_uuid, "tgt": tgt_uuid},
        )
        await _conn.execute(
            f"MATCH (a:Entity), (b:Entity) WHERE a.uuid = $src AND b.uuid = $tgt "
            f"CREATE (a)-[:Relation {{relation: '{new}', weight: {weight!r}, "
            f"description: '{desc}', created_at: {now!r}, superseded_at: null}}]->(b)",
            parameters={"src": src_uuid, "tgt": tgt_uuid},
        )
    return f"superseded {old_relation} -> {new_relation} from {src_uuid[:8]} -> {tgt_uuid[:8]}"


async def kg_delete_entity(uuid: str) -> str:
    """
    Hard-delete an entity and all its edges. Use sparingly.

    Args:
        uuid: The entity UUID to delete.
    """
    async with _write_lock:
        await _conn.execute(
            "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
            parameters={"uid": uuid},
        )
    return f"deleted entity {uuid[:8]}"


async def kg_delete_relationship(src_uuid: str, tgt_uuid: str, relation: str) -> str:
    """
    Delete all active edges of a given relation type between two entities.

    Args:
        src_uuid: Source entity UUID.
        tgt_uuid: Target entity UUID.
        relation: The relation label to delete.
    """
    rel = relation.upper().replace("'", "")
    async with _write_lock:
        await _conn.execute(
            f"MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            f"WHERE a.uuid = $src AND b.uuid = $tgt "
            f"AND r.relation = '{rel}' AND r.superseded_at IS NULL DELETE r",
            parameters={"src": src_uuid, "tgt": tgt_uuid},
        )
    return f"deleted {relation} from {src_uuid[:8]} -> {tgt_uuid[:8]}"


async def kg_find_entity(name: str = "", entity_type: str = "") -> str:
    """
    Search for entities by name substring and/or type. Use before kg_add_entity
    to avoid creating duplicates.

    Args:
        name: Partial name to search for (case-sensitive substring match).
        entity_type: Filter by entity type (exact match, optional).
    """
    if name and entity_type:
        r = await _conn.execute(
            "MATCH (e:Entity) WHERE e.name CONTAINS $name AND e.entity_type = $et "
            "RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
            parameters={"name": name, "et": entity_type},
        )
    elif name:
        r = await _conn.execute(
            "MATCH (e:Entity) WHERE e.name CONTAINS $name "
            "RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
            parameters={"name": name},
        )
    elif entity_type:
        r = await _conn.execute(
            "MATCH (e:Entity) WHERE e.entity_type = $et "
            "RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
            parameters={"et": entity_type},
        )
    else:
        return "[provide name or entity_type]"
    rows = []
    while r.has_next():
        row = r.get_next()
        rows.append(f"uuid={row[0]} name={row[1]} type={row[2]}\n  {row[3]}")
    return "\n\n".join(rows) if rows else "[no entities found]"


async def kg_search(query: str, top_k: int = 5, semantic: bool = True) -> str:
    """
    Search the knowledge graph for entities relevant to a query.
    Returns matching entities with their direct active relationships.
    Bumps mention_count on returned nodes.

    Args:
        query: Natural language query or keywords to search for.
        top_k: Maximum number of entities to return (default 5).
        semantic: If true (default), use vector similarity search.
            If false or no embedding model configured, uses keyword search.
    """
    query_vec = None
    if semantic and _embedder is not None:
        try:
            query_vec = await _embedder.embed_one(query)
        except Exception as exc:
            logger.warning("[memory] kg_search embed failed: %s -- falling back to keyword", exc)

    if query_vec is not None:
        all_embs = _graph_db.all_entities_with_embeddings()
        uids     = [uid for uid, _ in top_k_cosine(query_vec, all_embs, top_k)]
    else:
        uids = [r["uuid"] for r in _graph_db.find_entity(name=query)[:top_k]]

    if not uids:
        return "[no matching entities found]"

    _graph_db.bump_mention_count(uids)

    lines = []
    for uid in uids:
        entity = _graph_db.get_entity(uid)
        if not entity:
            continue
        lines.append(
            f"[{entity.get('e.entity_type', '?')}] {entity.get('e.name', '?')} (uuid: {uid[:8]})\n"
            f"  {entity.get('e.description', '')}"
        )
        for edge in entity.get("edges_out", []):
            lines.append(f"  ->[{edge['relation']}]-> {edge['target_name']}")
        for edge in entity.get("edges_in", []):
            lines.append(f"  <-[{edge['relation']}]<- {edge['source_name']}")

    return "\n\n".join(lines) if lines else "[no entities found]"


async def kg_traverse(uuid: str, hops: int = 1, relation_filter: str = "") -> str:
    """
    Walk the graph from an entity outward up to N hops.
    Returns all active edges encountered.

    Args:
        uuid: Starting entity UUID.
        hops: Number of hops to traverse (default 1, max 3).
        relation_filter: If provided, only follow edges with this relation label.
    """
    hops  = min(int(hops), 3)
    edges = _graph_db.traverse(uuid, hops, relation_filter or None)
    if not edges:
        return f"[no edges found from {uuid[:8]}]"
    lines = [f"Traversal from {uuid[:8]} ({hops} hop(s)):"]
    for e in edges:
        src = e.get("source_uuid", uuid)[:8]
        lines.append(f"  {src} ->[{e['relation']}]-> {e['target_name']} ({e['target_uuid'][:8]})")
    return "\n".join(lines)


async def kg_get_entity(uuid: str) -> str:
    """
    Retrieve full details of a knowledge graph entity including all
    active incoming and outgoing relationships.

    Args:
        uuid: The entity UUID to retrieve.
    """
    entity = _graph_db.get_entity(uuid)
    if not entity:
        return f"[entity {uuid[:8]} not found]"

    lines = [
        f"[{entity.get('e.entity_type', '?')}] {entity.get('e.name', '?')}",
        f"uuid: {uuid}",
        f"description: {entity.get('e.description', '')}",
        f"pinned: {entity.get('e.pinned')}  priority: {entity.get('e.priority')}  mentions: {entity.get('e.mention_count')}",
    ]
    for e in entity.get("edges_out", []):
        lines.append(
            f"  ->[{e['relation']}]-> {e['target_name']} ({e['target_uuid'][:8]})"
            + (f" -- {e['description']}" if e.get("description") else "")
        )
    for e in entity.get("edges_in", []):
        lines.append(
            f"  <-[{e['relation']}]<- {e['source_name']} ({e['source_uuid'][:8]})"
            + (f" -- {e['description']}" if e.get("description") else "")
        )
    return "\n".join(lines)


async def kg_list(entity_type: str = "", pinned_only: bool = False) -> str:
    """
    List knowledge graph entities, optionally filtered by type or pinned status.

    Args:
        entity_type: Filter by type (e.g. Person, Project, Technology). Empty = all types.
        pinned_only: If true, return only pinned entities.
    """
    entities = _graph_db.list_entities(entity_type=entity_type or None, pinned_only=pinned_only)
    if not entities:
        return "[no entities found]"
    lines = []
    for e in entities:
        pin = "[pinned] " if e.get("pinned") else ""
        lines.append(
            f"{pin}[{e['entity_type']}] {e['name']} ({e['uuid'][:8]}) pri={e['priority']}\n"
            f"  {e['description']}"
        )
    return "\n\n".join(lines)


async def kg_stats() -> str:
    """
    Show knowledge graph statistics: entity count, edge count, breakdown by type.
    """
    stats = _graph_db.get_stats()
    lines = [
        f"Entities: {stats['entity_count']}",
        f"Active edges: {stats['active_edge_count']}",
        "By type:",
    ] + [f"  {etype}: {count}" for etype, count in stats["by_type"].items()]
    return "\n".join(lines)

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
_bm25_weight:    float = 0.4


def init(
    conn,
    write_lock: asyncio.Lock,
    graph_db,
    embedder,
    *,
    query_template: str = "{text}",
    doc_template: str = "{text}",
    bm25_weight: float = 0.4,
):
    global _conn, _write_lock, _graph_db, _embedder, _query_template, _doc_template, _bm25_weight
    _conn             = conn
    _write_lock       = write_lock
    _graph_db         = graph_db
    _embedder         = embedder
    _query_template   = query_template
    _doc_template     = doc_template
    _bm25_weight      = max(0.0, min(1.0, bm25_weight))


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
        description: text info.
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
        return f"Warning: kg_update_entity called with no fields — nothing changed for UUID {uuid}. You must provide at least one of: description, priority, pinned."

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
            lines.append("  Warning: description passed to kg_update_entity is identical to the existing description — no change made. Write a genuinely updated description.")
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


async def kg_search(query: str, top_k: int = 3, semantic: bool = True) -> str:
    """
    Search the knowledge graph for entities relevant to a query.
    Uses hybrid BM25 + vector search fused with Reciprocal Rank Fusion (RRF).
    Returns matching entities with their direct active relationships.
    Bumps mention_count on returned nodes.

    Use this to check whether an entity already exists before calling
    kg_add_entity.

    Args:
        query: Natural language query or keywords to search for.
        top_k: Maximum number of entities to return (default 3).
        semantic: If true (default), include vector similarity search in fusion.
            If false or no embedding model configured, uses BM25 only.
    """
    from TinyCTX.utils.bm25 import BM25

    RRF_K = 60  # standard RRF constant

    # --- Exact name match (always pinned to top) ---
    exact_matches = _graph_db.find_entity(name=query)
    exact = next((e for e in exact_matches if e["name"].lower() == query.lower()), None)
    exact_uid = exact["uuid"] if exact else None

    # --- BM25 ---
    bm25_corpus = _graph_db.all_entities_for_bm25()
    bm25_scores: dict[str, int] = {}  # uid -> rank (1-based)
    if bm25_corpus:
        corpus_dict = {uid: text for uid, text in bm25_corpus}
        bm25 = BM25(corpus_dict)
        bm25_hits = bm25.search(query, top_k=len(corpus_dict))
        for rank, (uid, score) in enumerate(bm25_hits, start=1):
            if score > 0:
                bm25_scores[uid] = rank

    # --- Vector ---
    vec_scores: dict[str, int] = {}  # uid -> rank (1-based)
    if semantic and _embedder is not None:
        try:
            query_vec = await _embedder.embed_one(_query_template.format(text=query), priority=5)
            all_embs  = _graph_db.all_entities_with_embeddings()
            vec_hits  = top_k_cosine(query_vec, all_embs, len(all_embs))
            for rank, (uid, score) in enumerate(vec_hits, start=1):
                vec_scores[uid] = rank
        except Exception as exc:
            logger.warning("[memory] kg_search embed failed: %s -- BM25 only", exc)

    # --- RRF fusion ---
    vec_weight = 1.0 - _bm25_weight
    all_uids = set(bm25_scores) | set(vec_scores)
    rrf: dict[str, float] = {}
    for uid in all_uids:
        score = 0.0
        if uid in bm25_scores:
            score += _bm25_weight / (RRF_K + bm25_scores[uid])
        if uid in vec_scores:
            score += vec_weight / (RRF_K + vec_scores[uid])
        rrf[uid] = score

    ranked = sorted(rrf, key=lambda u: rrf[u], reverse=True)

    # Prepend exact match, deduplicate, cap at top_k
    if exact_uid:
        ranked = [exact_uid] + [u for u in ranked if u != exact_uid]
    uids = ranked[:top_k]

    if not uids:
        return "No matching entities found."

    _graph_db.bump_mention_count(uids)
    _graph_db.bump_last_read(uids, now_ts())

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
        pin_note   = f"  [pinned:{pin}]" if pin else ""
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


def _resolve_entity(uuid_or_name: str) -> dict | None:
    """Return entity_slim dict by UUID or exact name, or None if not found."""
    e = _graph_db.get_entity_slim(uuid_or_name)
    if e:
        return e
    matches = _graph_db.find_entity(name=uuid_or_name)
    exact = next((m for m in matches if m["name"].lower() == uuid_or_name.lower()), None)
    return exact  # already a slim dict (uuid, name, entity_type)


async def kg_merge_entities(
    canonical: str,
    duplicate: str,
    merged_description: str,
    verdict: str = "duplicate",
) -> str:
    """
    Merge two knowledge graph entities.

    Use when you identify that two nodes refer to the same real-world thing.
    All edges from the duplicate are re-pointed to the canonical node, then
    the duplicate is deleted (verdict="duplicate") or linked via ALIASED_TO
    (verdict="alias").

    Args:
        canonical: UUID or exact name of the node to keep as the authoritative entity.
        duplicate: UUID or exact name of the node to absorb or alias.
        merged_description: Consolidated description combining facts from both nodes.
        verdict: "duplicate" (delete the dup, reparent its edges) or
                 "alias" (keep both, add ALIASED_TO edge from dup to canonical).
    """
    if verdict not in ("duplicate", "alias"):
        return f"Error: verdict must be 'duplicate' or 'alias', got '{verdict}'."

    canon_e = _resolve_entity(canonical)
    dup_e   = _resolve_entity(duplicate)
    if not canon_e:
        return f"Error: canonical entity '{canonical}' not found."
    if not dup_e:
        return f"Error: duplicate entity '{duplicate}' not found."

    canonical_uuid = canon_e["uuid"]
    duplicate_uuid = dup_e["uuid"]

    if canonical_uuid == duplicate_uuid:
        return "Error: canonical and duplicate resolve to the same entity."

    canon_name = canon_e["name"]
    dup_name   = dup_e["name"]
    now        = now_ts()

    async with _write_lock:
        if verdict == "duplicate":
            await _aset(canonical_uuid, "description", merged_description)
            await _aset(canonical_uuid, "updated_at",  now)
            await _aset(canonical_uuid, "embed_hash",  "")
            # Re-point outgoing edges from dup to canon
            await _conn.execute(
                "MATCH (dup:Entity)-[r:Relation]->(x:Entity), (c:Entity) "
                "WHERE dup.uuid = $dup AND r.superseded_at IS NULL "
                "AND x.uuid <> $canon AND c.uuid = $canon "
                "CREATE (c)-[:Relation {relation: r.relation, weight: r.weight, "
                "description: r.description, created_at: r.created_at, superseded_at: null}]->(x)",
                parameters={"dup": duplicate_uuid, "canon": canonical_uuid},
            )
            # Re-point incoming edges to dup over to canon
            await _conn.execute(
                "MATCH (x:Entity)-[r:Relation]->(dup:Entity), (c:Entity) "
                "WHERE dup.uuid = $dup AND r.superseded_at IS NULL "
                "AND x.uuid <> $canon AND c.uuid = $canon "
                "CREATE (x)-[:Relation {relation: r.relation, weight: r.weight, "
                "description: r.description, created_at: r.created_at, superseded_at: null}]->(c)",
                parameters={"dup": duplicate_uuid, "canon": canonical_uuid},
            )
            await _conn.execute(
                "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
                parameters={"uid": duplicate_uuid},
            )
            return (
                f"Merged '{dup_name}' ({duplicate_uuid}) into '{canon_name}' ({canonical_uuid}).\n"
                f"  Edges reparented and duplicate deleted.\n"
                f"  Description: {merged_description}"
            )
        else:  # alias
            await _aset(duplicate_uuid, "description", f"This node is aliased to {canon_name}. The UUID for that node is {canonical_uuid}.")
            await _aset(duplicate_uuid, "updated_at",  now)
            await _aset(canonical_uuid, "description", merged_description)
            await _aset(canonical_uuid, "updated_at",  now)
            await _aset(canonical_uuid, "embed_hash",  "")
            await _conn.execute(
                f"MATCH (a:Entity), (c:Entity) "
                f"WHERE a.uuid = $alias AND c.uuid = $canon "
                f"CREATE (a)-[:Relation {{relation: 'ALIASED_TO', weight: 1.0, "
                f"description: 'alias', created_at: {now!r}, superseded_at: null}}]->(c)",
                parameters={"alias": duplicate_uuid, "canon": canonical_uuid},
            )
            return (
                f"Aliased '{dup_name}' ({duplicate_uuid}) -> '{canon_name}' ({canonical_uuid}).\n"
                f"  Canonical description updated: {merged_description}\n"
                f"  Alias node description set to redirect stub."
            )

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

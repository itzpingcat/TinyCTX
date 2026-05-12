"""
modules/memory/librarian_agents.py

Pure agent logic for the knowledge librarian: buffer ingestion, targeted
edits, and dedup. No process management, no IPC, no event loop ownership.

Called by LibrarianRunner (_poll_cycle) in __main__.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


async def get_relation_types(conn) -> str:
    """Return a comma-separated string of relation types: defaults union live graph labels."""
    defaults = [
        t.strip()
        for line in (_PROMPTS_DIR / "default_relation_types.txt").read_text(encoding="utf-8").splitlines()
        for t in line.split(",")
        if t.strip()
    ]
    r = await conn.execute(
        "MATCH ()-[r:Relation]->() WHERE r.superseded_at IS NULL RETURN DISTINCT r.relation ORDER BY r.relation"
    )
    live = []
    while r.has_next():
        live.append(r.get_next()[0])
    extras = [l for l in live if l not in defaults]
    return ", ".join(defaults + extras)

def _set(conn, uid: str, field: str, value):
    """Issue a single-field SET via two-param query (uuid + value).
    Ladybug's param binder works reliably for exactly 2 params."""
    return conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


async def _aset(conn, uid: str, field: str, value):
    """Async version of _set."""
    return await conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


# ---------------------------------------------------------------------------
# Conversation node → text
# ---------------------------------------------------------------------------

def nodes_to_text(conv_db, node_ids: list[str], batch_size: int) -> str:
    """Render up to batch_size nodes as '[author]: content' lines."""
    lines: list[str] = []
    for node_id in node_ids[:batch_size]:
        node = conv_db.get_node(node_id)
        if node is None or node.role not in ("user", "assistant"):
            continue
        author  = node.author_name or node.author_id or node.role
        content = node.content or ""
        if content.startswith("["):
            try:
                blocks = json.loads(content)
                texts  = [b.get("text", "") for b in blocks
                          if isinstance(b, dict) and b.get("type") == "text"]
                content = " ".join(texts)
            except Exception:
                pass
        content = content.strip()
        if content:
            lines.append(f"[{author}]: {content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Buffer agent
# ---------------------------------------------------------------------------

async def run_buffer_agent(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    batch_text: str,
    agent_logger: logging.Logger,
) -> None:
    """Ingest a batch of conversation nodes into the knowledge graph."""
    write_tools = _make_write_tools(conn, write_lock)
    read_tools  = _make_read_tools(conn)

    relation_vocab = await get_relation_types(conn)

    await _agent_loop(
        llm,
        _prompt("buffer_system.txt").format(relation_vocab=relation_vocab),
        _prompt("buffer_user.txt").format(batch_text=batch_text),
        write_tools + read_tools,
        agent_logger,
    )


# ---------------------------------------------------------------------------
# Targeted agent
# ---------------------------------------------------------------------------

async def run_targeted_agent(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    prompt: str,
    agent_logger: logging.Logger,
) -> None:
    """Execute a specific graph-edit instruction."""
    write_tools = _make_write_tools(conn, write_lock)
    read_tools  = _make_read_tools(conn)

    relation_vocab = await get_relation_types(conn)

    await _agent_loop(
        llm,
        _prompt("targeted_system.txt").format(relation_vocab=relation_vocab),
        prompt,
        write_tools + read_tools,
        agent_logger,
    )


# ---------------------------------------------------------------------------
# Dedup cycle
# ---------------------------------------------------------------------------

async def run_dedup_cycle(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    embedder,
    agent_logger: logging.Logger,
) -> None:
    logger.info("[memory/librarian] dedup cycle starting")
    try:
        from TinyCTX.modules.memory.graph import (
            embed_content_for, embed_hash, cosine_similarity, now_ts,
        )

        threshold = float(cfg.get("similarity_threshold", 0.85))

        r = await conn.execute(
            "MATCH (e:Entity) RETURN e.uuid, e.name, e.description, e.entity_type, "
            "e.embed_model, e.embed_hash, e.embedding"
        )
        col_names = r.get_column_names()
        entities  = []
        while r.has_next():
            entities.append(dict(zip(col_names, r.get_next())))

        if len(entities) < 2:
            logger.info("[memory/librarian] dedup: fewer than 2 entities, skipping")
            return

        embed_model_name = getattr(embedder, "model", "")
        stale = []
        for e in entities:
            expected_hash = embed_hash(embed_content_for(e["e.name"], e["e.description"]))
            if (
                not e["e.embedding"]
                or e["e.embed_model"] != embed_model_name
                or e["e.embed_hash"] != expected_hash
            ):
                stale.append(e)

        if stale:
            logger.info("[memory/librarian] dedup: refreshing %d stale embedding(s)", len(stale))
            texts   = [embed_content_for(e["e.name"], e["e.description"]) for e in stale]
            vectors = await embedder.embed(texts)
            async with write_lock:
                for e, vec, txt in zip(stale, vectors, texts):
                    h   = embed_hash(txt)
                    uid = e["e.uuid"]
                    await _aset(conn, uid, "embedding",    vec)
                    await _aset(conn, uid, "embed_model",  embed_model_name)
                    await _aset(conn, uid, "embed_content", txt)
                    await _aset(conn, uid, "embed_hash",   h)
                    e["e.embedding"]   = vec
                    e["e.embed_model"] = embed_model_name
                    e["e.embed_hash"]  = h

        pairs_seen: set[frozenset] = set()
        candidates: list[tuple[dict, dict, float]] = []

        for i, ea in enumerate(entities):
            emb_a = ea.get("e.embedding") or []
            if not emb_a:
                continue
            for eb in entities[i + 1:]:
                emb_b = eb.get("e.embedding") or []
                if not emb_b:
                    continue
                pair_key = frozenset([ea["e.uuid"], eb["e.uuid"]])
                if pair_key in pairs_seen:
                    continue
                pairs_seen.add(pair_key)
                score = cosine_similarity(emb_a, emb_b)
                if score >= threshold:
                    candidates.append((ea, eb, score))

        if not candidates:
            logger.info("[memory/librarian] dedup: no candidate pairs above threshold %.2f", threshold)
            return

        logger.info("[memory/librarian] dedup: %d candidate pair(s) to evaluate", len(candidates))

        already_aliased: set[frozenset] = set()
        r = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            "WHERE r.relation = 'ALIASED_TO' AND r.superseded_at IS NULL "
            "RETURN a.uuid, b.uuid"
        )
        while r.has_next():
            row = r.get_next()
            already_aliased.add(frozenset([row[0], row[1]]))

        for ea, eb, _score in candidates:
            pair_key = frozenset([ea["e.uuid"], eb["e.uuid"]])
            if pair_key in already_aliased:
                continue
            await _dedup_pair(conn, write_lock, llm, ea, eb, agent_logger)

        logger.info("[memory/librarian] dedup cycle complete")
    except Exception:
        logger.exception("[memory/librarian] dedup cycle error")


async def _dedup_pair(conn, write_lock: asyncio.Lock, llm, ea: dict, eb: dict, agent_logger: logging.Logger) -> None:
    from TinyCTX.modules.memory.graph import now_ts
    from TinyCTX.ai import TextDelta

    prompt = _prompt("dedup_user.txt").format(
        uuid_a=ea["e.uuid"], name_a=ea["e.name"],
        type_a=ea["e.entity_type"], desc_a=ea["e.description"],
        uuid_b=eb["e.uuid"], name_b=eb["e.name"],
        type_b=eb["e.entity_type"], desc_b=eb["e.description"],
    )

    response_text = ""
    async for event in llm.stream([{"role": "user", "content": prompt}], tools=None):
        if isinstance(event, TextDelta):
            response_text += event.text

    if response_text:
        agent_logger.info("[dedup %s/%s] %s", ea["e.uuid"][:8], eb["e.uuid"][:8], response_text)

    raw = _re.sub(r"^```json?\s*", "", response_text.strip())
    raw = _re.sub(r"\s*```$", "", raw)

    try:
        verdict_data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "[memory/librarian] dedup: could not parse verdict for %s/%s: %s",
            ea["e.uuid"][:8], eb["e.uuid"][:8], raw[:200],
        )
        return

    verdict        = verdict_data.get("verdict", "distinct")
    canonical_uuid = verdict_data.get("canonical_uuid")
    merged_desc    = verdict_data.get("merged_description", "")

    if verdict == "distinct":
        return

    if not canonical_uuid or canonical_uuid not in {ea["e.uuid"], eb["e.uuid"]}:
        logger.warning("[memory/librarian] dedup: invalid canonical_uuid in verdict")
        return

    dup_uuid = eb["e.uuid"] if canonical_uuid == ea["e.uuid"] else ea["e.uuid"]
    now      = now_ts()

    async with write_lock:
        if verdict == "duplicate":
            logger.info("[memory/librarian] dedup: merging %s → %s", dup_uuid[:8], canonical_uuid[:8])
            await _aset(conn, canonical_uuid, "description", merged_desc)
            await _aset(conn, canonical_uuid, "updated_at",  now)
            await _aset(conn, canonical_uuid, "embed_hash",  "")
            # Copy outgoing edges from dup to canonical
            await conn.execute(
                "MATCH (dup:Entity)-[r:Relation]->(x:Entity), (c:Entity) "
                "WHERE dup.uuid = $dup AND r.superseded_at IS NULL "
                "AND x.uuid <> $canon AND c.uuid = $canon "
                "CREATE (c)-[:Relation {relation: r.relation, weight: r.weight, "
                "description: r.description, created_at: r.created_at, superseded_at: null}]->(x)",
                parameters={"dup": dup_uuid, "canon": canonical_uuid},
            )
            # Copy incoming edges from dup to canonical
            await conn.execute(
                "MATCH (x:Entity)-[r:Relation]->(dup:Entity), (c:Entity) "
                "WHERE dup.uuid = $dup AND r.superseded_at IS NULL "
                "AND x.uuid <> $canon AND c.uuid = $canon "
                "CREATE (x)-[:Relation {relation: r.relation, weight: r.weight, "
                "description: r.description, created_at: r.created_at, superseded_at: null}]->(c)",
                parameters={"dup": dup_uuid, "canon": canonical_uuid},
            )
            await conn.execute(
                "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
                parameters={"uid": dup_uuid},
            )
        elif verdict == "alias":
            logger.info("[memory/librarian] dedup: aliasing %s → %s", dup_uuid[:8], canonical_uuid[:8])
            await _aset(conn, dup_uuid, "description", merged_desc)
            await _aset(conn, dup_uuid, "updated_at",  now)
            await conn.execute(
                f"MATCH (a:Entity), (c:Entity) "
                f"WHERE a.uuid = $alias AND c.uuid = $canon "
                f"CREATE (a)-[:Relation {{relation: 'ALIASED_TO', weight: 1.0, "
                f"description: 'alias', created_at: {now!r}, superseded_at: null}}]->(c)",
                parameters={"alias": dup_uuid, "canon": canonical_uuid},
            )


# ---------------------------------------------------------------------------
# Graph write tools
# ---------------------------------------------------------------------------

def _make_write_tools(conn, write_lock: asyncio.Lock) -> list[dict]:
    from TinyCTX.modules.memory.graph import new_uuid, now_ts
    tools = []

    async def add_entity(
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
        r = await conn.execute(
            "MATCH (e:Entity) WHERE e.name = $name AND e.entity_type = $et RETURN e.uuid LIMIT 1",
            parameters={"name": name, "et": entity_type},
        )
        if r.has_next():
            uid = r.get_next()[0]
            async with write_lock:
                await _aset(conn, uid, "description",  description)
                await _aset(conn, uid, "updated_at",   now)
                await _aset(conn, uid, "priority",     priority)
                await _aset(conn, uid, "pinned",       pinned)
                await _aset(conn, uid, "embed_hash",   "")
            return uid
        uid = new_uuid()
        async with write_lock:
            await conn.execute(
                "CREATE (e:Entity {uuid: $uid})",
                parameters={"uid": uid},
            )
            await _aset(conn, uid, "name",          name)
            await _aset(conn, uid, "entity_type",   entity_type)
            await _aset(conn, uid, "description",   description)
            await _aset(conn, uid, "pinned",        pinned)
            await _aset(conn, uid, "priority",      priority)
            await _aset(conn, uid, "mention_count", 0)
            await _aset(conn, uid, "created_at",    now)
            await _aset(conn, uid, "updated_at",    now)
            await _aset(conn, uid, "embed_model",   "")
            await _aset(conn, uid, "embed_content", "")
            await _aset(conn, uid, "embed_hash",    "")
        return uid

    async def update_entity(
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
        now  = now_ts()
        if description is None and priority is None and pinned is None:
            return f"[no fields to update for {uuid}]"
        async with write_lock:
            if description is not None:
                await _aset(conn, uuid, "description", description)
                await _aset(conn, uuid, "embed_hash",  "")
            if priority is not None:
                await _aset(conn, uuid, "priority", priority)
            if pinned is not None:
                await _aset(conn, uuid, "pinned", pinned)
            await _aset(conn, uuid, "updated_at", now)
        return f"updated {uuid}"

    async def add_relationship(
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
        now = now_ts()
        rel  = relation.upper().replace("'", "")
        desc = description.replace("'", "''")
        async with write_lock:
            await conn.execute(
                f"MATCH (a:Entity), (b:Entity) WHERE a.uuid = $src AND b.uuid = $tgt "
                f"CREATE (a)-[:Relation {{relation: '{rel}', weight: {weight!r}, "
                f"description: '{desc}', created_at: {now!r}, superseded_at: null}}]->(b)",
                parameters={"src": source_uuid, "tgt": target_uuid},
            )
        return f"added {relation} from {source_uuid[:8]} → {target_uuid[:8]}"

    async def supersede_relationship(
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
        async with write_lock:
            await conn.execute(
                f"MATCH (a:Entity)-[r:Relation]->(b:Entity) "
                f"WHERE a.uuid = $src AND b.uuid = $tgt "
                f"AND r.relation = '{old}' AND r.superseded_at IS NULL "
                f"SET r.superseded_at = {now!r}",
                parameters={"src": src_uuid, "tgt": tgt_uuid},
            )
            await conn.execute(
                f"MATCH (a:Entity), (b:Entity) WHERE a.uuid = $src AND b.uuid = $tgt "
                f"CREATE (a)-[:Relation {{relation: '{new}', weight: {weight!r}, "
                f"description: '{desc}', created_at: {now!r}, superseded_at: null}}]->(b)",
                parameters={"src": src_uuid, "tgt": tgt_uuid},
            )
        return f"superseded {old_relation} → {new_relation} from {src_uuid[:8]} → {tgt_uuid[:8]}"

    async def delete_entity(uuid: str) -> str:
        """
        Hard-delete an entity and all its edges. Use sparingly.

        Args:
            uuid: The entity UUID to delete.
        """
        async with write_lock:
            await conn.execute(
                "MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e",
                parameters={"uid": uuid},
            )
        return f"deleted entity {uuid[:8]}"

    async def delete_relationship(src_uuid: str, tgt_uuid: str, relation: str) -> str:
        """
        Delete all active edges of a given relation type between two entities.

        Args:
            src_uuid: Source entity UUID.
            tgt_uuid: Target entity UUID.
            relation: The relation label to delete.
        """
        rel = relation.upper().replace("'", "")
        async with write_lock:
            await conn.execute(
                f"MATCH (a:Entity)-[r:Relation]->(b:Entity) "
                f"WHERE a.uuid = $src AND b.uuid = $tgt "
                f"AND r.relation = '{rel}' AND r.superseded_at IS NULL DELETE r",
                parameters={"src": src_uuid, "tgt": tgt_uuid},
            )
        return f"deleted {relation} from {src_uuid[:8]} → {tgt_uuid[:8]}"

    for fn in [
        add_entity, update_entity, add_relationship,
        supersede_relationship, delete_entity, delete_relationship,
    ]:
        tools.append({"fn": fn, "name": fn.__name__, "doc": fn.__doc__})

    return tools


# ---------------------------------------------------------------------------
# Graph read tools
# ---------------------------------------------------------------------------

def _make_read_tools(conn) -> list[dict]:
    tools = []

    async def find_entity(name: str = "", entity_type: str = "") -> str:
        """
        Search for entities by name substring and/or type. Use before add_entity
        to avoid creating duplicates.

        Args:
            name: Partial name to search for (case-sensitive substring match).
            entity_type: Filter by entity type (exact match, optional).
        """
        if name and entity_type:
            r = await conn.execute(
                "MATCH (e:Entity) WHERE e.name CONTAINS $name AND e.entity_type = $et "
                "RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
                parameters={"name": name, "et": entity_type},
            )
        elif name:
            r = await conn.execute(
                "MATCH (e:Entity) WHERE e.name CONTAINS $name "
                "RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
                parameters={"name": name},
            )
        elif entity_type:
            r = await conn.execute(
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

    async def get_entity(uuid: str) -> str:
        """
        Get full details of an entity including all active relationships.

        Args:
            uuid: The entity UUID to retrieve.
        """
        r = await conn.execute(
            "MATCH (e:Entity {uuid: $uid}) RETURN e.*",
            parameters={"uid": uuid},
        )
        if not r.has_next():
            return f"[entity {uuid[:8]} not found]"
        row  = r.get_next()
        cols = r.get_column_names()
        data = dict(zip(cols, row))
        data.pop("e.embedding", None)

        edges_out = await conn.execute(
            "MATCH (a:Entity {uuid: $uid})-[r:Relation]->(b:Entity) "
            "WHERE r.superseded_at IS NULL "
            "RETURN b.uuid, b.name, r.relation, r.weight",
            parameters={"uid": uuid},
        )
        edges_in = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity {uuid: $uid}) "
            "WHERE r.superseded_at IS NULL "
            "RETURN a.uuid, a.name, r.relation, r.weight",
            parameters={"uid": uuid},
        )

        out_lines, in_lines = [], []
        while edges_out.has_next():
            row = edges_out.get_next()
            out_lines.append(f"  →[{row[2]}]→ {row[1]} ({row[0][:8]}) weight={row[3]}")
        while edges_in.has_next():
            row = edges_in.get_next()
            in_lines.append(f"  ←[{row[2]}]← {row[1]} ({row[0][:8]}) weight={row[3]}")

        lines = [
            f"Entity: {data.get('e.name')} [{data.get('e.entity_type')}]",
            f"uuid: {uuid}",
            f"description: {data.get('e.description')}",
            f"pinned: {data.get('e.pinned')}  priority: {data.get('e.priority')}",
        ]
        if out_lines:
            lines.append("outgoing:")
            lines.extend(out_lines)
        if in_lines:
            lines.append("incoming:")
            lines.extend(in_lines)
        return "\n".join(lines)

    for fn in [find_entity, get_entity]:
        tools.append({"fn": fn, "name": fn.__name__, "doc": fn.__doc__})
    return tools


# ---------------------------------------------------------------------------
# Minimal agent loop
# ---------------------------------------------------------------------------

async def _agent_loop(
    llm,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    agent_logger: logging.Logger,
    max_cycles: int = 20,
) -> None:
    import inspect
    from TinyCTX.ai import TextDelta, ToolCallAssembled, LLMError

    tool_defs = []
    tool_map  = {}
    for t in tools:
        sig   = inspect.signature(t["fn"])
        props: dict = {}
        required: list = []
        for pname, param in sig.parameters.items():
            ann   = param.annotation
            ptype = (
                "integer" if ann is int else
                "number"  if ann is float else
                "boolean" if ann is bool else
                "string"
            )
            props[pname] = {"type": ptype, "description": ""}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        tool_defs.append({
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": (t["doc"] or "").strip().split("\n\n")[0][:200],
                "parameters":  {"type": "object", "properties": props, "required": required},
            },
        })
        tool_map[t["name"]] = t["fn"]

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    for cycle in range(max_cycles):
        text_chunks: list[str] = []
        tool_calls:  list[dict] = []

        async for event in llm.stream(messages, tools=tool_defs):
            if isinstance(event, TextDelta):
                text_chunks.append(event.text)
            elif isinstance(event, ToolCallAssembled):
                tool_calls.append({"id": event.call_id, "name": event.tool_name, "args": event.args})
            elif isinstance(event, LLMError):
                logger.error("[memory/librarian] LLM error: %s", event.message)
                return

        response_text = "".join(text_chunks)

        if response_text:
            label = "[final]" if not tool_calls else f"[cycle {cycle}]"
            agent_logger.info("%s %s", label, response_text)

        if not tool_calls:
            return

        messages.append({
            "role":    "assistant",
            "content": response_text,
            "tool_calls": [
                {
                    "id":   tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            fn = tool_map.get(tc["name"])
            if fn is None:
                result = f"[unknown tool: {tc['name']}]"
            else:
                try:
                    result = await fn(**tc["args"])
                except Exception as exc:
                    result = f"[error: {exc}]"
                    logger.warning("[memory/librarian] tool %s error: %s", tc["name"], exc)

            agent_logger.debug("  tool %s → %s", tc["name"], result)
            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      str(result),
            })

    logger.warning("[memory/librarian] hit max_cycles (%d)", max_cycles)

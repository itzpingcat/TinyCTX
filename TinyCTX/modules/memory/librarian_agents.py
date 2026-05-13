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


async def _aset(conn, uid: str, field: str, value):
    """Async single-field SET."""
    return await conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


# ---------------------------------------------------------------------------
# Conversation node → text
# ---------------------------------------------------------------------------

def nodes_to_text(conv_db, node_ids: list[str], batch_size: int) -> tuple[str, str]:
    """
    Render up to batch_size nodes as '[author]: content' lines.
    Returns (batch_text, agent_name) where agent_name is the last assistant
    author_name seen in the batch, or 'assistant' if none found.
    """
    lines: list[str] = []
    agent_name = "assistant"
    for node_id in node_ids[:batch_size]:
        node = conv_db.get_node(node_id)
        if node is None or node.role not in ("user", "assistant"):
            continue
        author  = node.author_name or node.author_id or node.role
        if node.role == "assistant" and node.author_name:
            agent_name = node.author_name
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
    return "\n".join(lines), agent_name


# ---------------------------------------------------------------------------
# Shared: build a ToolCallHandler for librarian agents
# ---------------------------------------------------------------------------

def _make_tool_handler():
    from TinyCTX.utils.tool_handler import ToolCallHandler
    import TinyCTX.modules.memory.tools as tools

    handler = ToolCallHandler()
    for fn in [
        tools.kg_add_entity,
        tools.kg_update_entity,
        tools.kg_add_relationship,
        tools.kg_supersede_relationship,
        tools.kg_delete_entity,
        tools.kg_delete_relationship,
        tools.kg_find_entity,
        tools.kg_get_entity,
    ]:
        handler.register_tool(fn, always_on=True, min_permission=25)
    return handler


# ---------------------------------------------------------------------------
# Buffer agent
# ---------------------------------------------------------------------------

async def run_buffer_agent(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    batch_text: str,
    agent_name: str,
    agent_logger: logging.Logger,
) -> None:
    """Ingest a batch of conversation nodes into the knowledge graph."""
    relation_vocab = await get_relation_types(conn)
    await _agent_loop(
        llm,
        _prompt("buffer_system.txt").format(
            relation_vocab=relation_vocab,
            agent_name=agent_name,
        ),
        _prompt("buffer_user.txt").format(batch_text=batch_text),
        _make_tool_handler(),
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
    relation_vocab = await get_relation_types(conn)
    await _agent_loop(
        llm,
        _prompt("targeted_system.txt").format(relation_vocab=relation_vocab),
        prompt,
        _make_tool_handler(),
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
            await conn.execute(
                "MATCH (dup:Entity)-[r:Relation]->(x:Entity), (c:Entity) "
                "WHERE dup.uuid = $dup AND r.superseded_at IS NULL "
                "AND x.uuid <> $canon AND c.uuid = $canon "
                "CREATE (c)-[:Relation {relation: r.relation, weight: r.weight, "
                "description: r.description, created_at: r.created_at, superseded_at: null}]->(x)",
                parameters={"dup": dup_uuid, "canon": canonical_uuid},
            )
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
# Agent loop
# ---------------------------------------------------------------------------

async def _agent_loop(
    llm,
    system_prompt: str,
    user_prompt: str,
    handler,
    agent_logger: logging.Logger,
    max_cycles: int = 20,
) -> None:
    from TinyCTX.ai import TextDelta, ToolCallAssembled, LLMError

    tool_defs = handler.get_tool_definitions(caller_level=25)
    messages  = [
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
            outcome = await handler.execute_tool_call(
                {"id": tc["id"], "function": {"name": tc["name"], "arguments": tc["args"]}},
                caller_level=25,
            )
            result = outcome["result"] if outcome["success"] else outcome["error"]
            agent_logger.debug("  tool %s → %s", tc["name"], result)
            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      str(result),
            })

    logger.warning("[memory/librarian] hit max_cycles (%d)", max_cycles)

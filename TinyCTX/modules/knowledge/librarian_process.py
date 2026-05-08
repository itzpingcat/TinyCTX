"""
modules/knowledge/librarian_process.py

Persistent sidecar process that manages all writes to the knowledge graph.

Launched by the knowledge module at agent startup. Runs its own asyncio
event loop, separate from the main agent's event loop.

Responsibilities
----------------
- Poll libbuffer/ for session files and process them via buffer agents
- Listen on the IPC socket for on-demand triggers from the main agent
- Run dedup cycles on a configurable schedule
- Manage the meeseeks lifecycle: spawn, run, discard librarian agents

Process lifecycle
-----------------
Writes knowledge/librarian.pid on startup. Reads the config dict from
argv[1] as JSON. Exits cleanly on SIGTERM/SIGINT, draining in-flight agents.

On startup the PID file is checked for a stale previous process:
  - If the PID is alive: exit immediately (another instance is running).
  - If the PID is dead (or file missing): overwrite and continue.

Agent types
-----------
buffer   — ingest a batch of messages from a buffer file
targeted — execute a specific graph-edit task from a prompt string

Dedup
-----
Not an agent. The librarian process makes direct LLM calls per candidate
pair and handles graph writes itself.

Write connection
----------------
A single ladybug.AsyncConnection is shared across all coroutines in this
process. ladybug handles internal concurrency; we serialise our own writes
via an asyncio.Lock to keep semantics simple.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: librarian_process.py <config_json>", file=sys.stderr)
        sys.exit(1)

    cfg = json.loads(sys.argv[1])
    _setup_logging(cfg.get("log_level", "INFO"))

    workspace    = Path(cfg["workspace"]).expanduser().resolve()
    graph_path   = workspace / cfg["graph_path"]
    libbuffer_dir = workspace / cfg["libbuffer_dir"]
    pid_file     = workspace / "knowledge" / "librarian.pid"
    sock_path    = workspace / cfg["ipc_socket"]

    # PID check — detect stale or duplicate
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)  # raises if dead
            logger.warning("[librarian] another instance is running (PID %d) — exiting", old_pid)
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass  # stale PID file

    pid_file.write_text(str(os.getpid()))
    logger.info("[librarian] started PID=%d", os.getpid())

    try:
        asyncio.run(_run(cfg, workspace, graph_path, libbuffer_dir, sock_path))
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("[librarian] exited")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------

async def _run(
    cfg: dict,
    workspace: Path,
    graph_path: Path,
    libbuffer_dir: Path,
    sock_path: Path,
) -> None:
    import ladybug as kuzu
    from TinyCTX.modules.knowledge.graph import init_schema, GraphDB

    # Open the write connection
    graph_path.mkdir(parents=True, exist_ok=True)
    libbuffer_dir.mkdir(parents=True, exist_ok=True)
    sock_path.parent.mkdir(parents=True, exist_ok=True)

    db   = kuzu.Database(str(graph_path))
    conn = kuzu.AsyncConnection(db)
    await conn.execute("RETURN 1")  # warm up / validate

    # Ensure schema exists
    sync_conn = kuzu.Connection(db)
    init_schema(sync_conn)

    write_lock = asyncio.Lock()

    # Active agent task set
    active_tasks: set[asyncio.Task] = set()

    # Timestamps of last runs
    state = {
        "last_buffer_ts":  0.0,
        "last_dedup_ts":   0.0,
        "dedup_running":   False,
    }

    # Files currently being processed (avoid double-processing)
    in_flight_files: set[Path] = set()

    # Build LLM client for librarian agents
    primary_model_cfg = cfg.get("primary_model", {})
    from TinyCTX.ai import LLM
    llm = LLM(
        base_url=primary_model_cfg.get("base_url", ""),
        api_key=primary_model_cfg.get("api_key", ""),
        model=primary_model_cfg.get("model", ""),
        max_tokens=primary_model_cfg.get("max_tokens", 2048),
        temperature=primary_model_cfg.get("temperature", 0.7),
    )

    # Embedder (optional)
    embedder = None
    embed_cfg = cfg.get("embed_model", {})
    if embed_cfg.get("model"):
        from TinyCTX.ai import Embedder
        embedder = Embedder(
            base_url=embed_cfg["base_url"],
            api_key=embed_cfg.get("api_key", ""),
            model=embed_cfg["model"],
        )

    # IPC server
    from TinyCTX.modules.knowledge.ipc import IPCServer
    ipc_queue: asyncio.Queue = asyncio.Queue()

    def _on_ipc_message(msg: dict) -> None:
        ipc_queue.put_nowait(msg)

    ipc_server = IPCServer(sock_path, _on_ipc_message)
    await ipc_server.start()

    # Graceful shutdown event
    shutdown_event = asyncio.Event()

    def _handle_signal(*_) -> None:
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows

    max_concurrent = int(cfg.get("max_concurrent", 4))
    batch_size     = int(cfg.get("batch_size", 20))

    logger.info("[librarian] polling loop started")

    while not shutdown_event.is_set():
        try:
            await _poll_cycle(
                cfg, conn, write_lock, llm, embedder,
                libbuffer_dir, in_flight_files, active_tasks,
                state, max_concurrent, batch_size, ipc_queue,
            )
        except Exception:
            logger.exception("[librarian] poll cycle error")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass

    # Drain in-flight agents
    if active_tasks:
        logger.info("[librarian] draining %d in-flight agent(s)…", len(active_tasks))
        await asyncio.gather(*active_tasks, return_exceptions=True)

    ipc_server.close()
    conn.close()
    logger.info("[librarian] shutdown complete")


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

async def _poll_cycle(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    embedder,
    libbuffer_dir: Path,
    in_flight_files: set[Path],
    active_tasks: set[asyncio.Task],
    state: dict,
    max_concurrent: int,
    batch_size: int,
    ipc_queue: asyncio.Queue,
) -> None:
    # Reap finished tasks
    done = {t for t in active_tasks if t.done()}
    for t in done:
        if t.exception():
            logger.error("[librarian] agent task raised: %s", t.exception())
    active_tasks -= done

    # Process IPC messages
    while not ipc_queue.empty():
        msg = ipc_queue.get_nowait()
        msg_type = msg.get("type")
        if msg_type == "targeted":
            prompt = msg.get("prompt", "").strip()
            if prompt and len(active_tasks) < max_concurrent:
                task = asyncio.create_task(
                    _run_targeted_agent(cfg, conn, write_lock, llm, prompt)
                )
                active_tasks.add(task)
            elif prompt:
                logger.warning("[librarian] targeted agent queued but concurrency cap reached")
        elif msg_type == "trigger":
            pass  # fall through to buffer trigger logic below

    # Buffer trigger logic
    now = time.time()
    file_size_limit = int(cfg.get("trigger_file_size_kb", 64)) * 1024
    interval_hours  = float(cfg.get("trigger_interval_hours", 6))
    interval_secs   = interval_hours * 3600
    time_elapsed    = (now - state["last_buffer_ts"]) >= interval_secs

    buffer_files = sorted(libbuffer_dir.glob("session_*.txt"))

    for bf in buffer_files:
        if bf in in_flight_files:
            continue
        if len(active_tasks) >= max_concurrent:
            break
        try:
            size = bf.stat().st_size
        except FileNotFoundError:
            continue

        should_trigger = size >= file_size_limit or (time_elapsed and size > 0)
        if not should_trigger:
            continue

        in_flight_files.add(bf)
        state["last_buffer_ts"] = now

        task = asyncio.create_task(
            _process_buffer_file(cfg, conn, write_lock, llm, bf, batch_size, in_flight_files)
        )
        active_tasks.add(task)

    # Dedup trigger
    dedup_enabled  = bool(cfg.get("dedup_enabled", True))
    dedup_interval = float(cfg.get("dedup_interval_hours", 24)) * 3600
    if (
        dedup_enabled
        and not state["dedup_running"]
        and (now - state["last_dedup_ts"]) >= dedup_interval
        and embedder is not None
    ):
        state["dedup_running"] = True
        state["last_dedup_ts"] = now
        task = asyncio.create_task(
            _run_dedup_cycle(cfg, conn, write_lock, llm, embedder)
        )
        task.add_done_callback(lambda _: state.__setitem__("dedup_running", False))
        active_tasks.add(task)


# ---------------------------------------------------------------------------
# Buffer agent
# ---------------------------------------------------------------------------

async def _process_buffer_file(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    buffer_file: Path,
    batch_size: int,
    in_flight_files: set[Path],
) -> None:
    logger.info("[librarian] processing buffer file: %s", buffer_file.name)
    try:
        while True:
            # Read oldest batch_size lines
            try:
                all_lines = buffer_file.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                break

            if not all_lines:
                break

            batch = all_lines[:batch_size]
            remaining = all_lines[batch_size:]

            # Remove those lines from the file
            if remaining:
                buffer_file.write_text("\n".join(remaining) + "\n", encoding="utf-8")
            else:
                buffer_file.unlink(missing_ok=True)

            # Spawn agent for this batch
            batch_text = "\n".join(batch)
            await _run_buffer_agent(cfg, conn, write_lock, llm, batch_text)

            if not remaining:
                break

    except Exception:
        logger.exception("[librarian] error processing buffer file %s", buffer_file.name)
    finally:
        in_flight_files.discard(buffer_file)
    logger.info("[librarian] done with buffer file: %s", buffer_file.name)


async def _run_buffer_agent(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    batch_text: str,
) -> None:
    """Run a buffer librarian agent for one batch of conversation lines."""
    write_tools = _make_write_tools(conn, write_lock)
    read_tools  = _make_read_tools(conn)
    tools       = write_tools + read_tools

    system_prompt = (
        "You are a knowledge extraction agent. Your task is to analyse conversation "
        "excerpts and update a knowledge graph by extracting entities and relationships.\n\n"
        "Rules:\n"
        "- Extract only explicitly stated facts — never infer or speculate.\n"
        "- Before inserting a new entity, use find_entity to check for existing matches. "
        "Reuse existing nodes when names/types match closely.\n"
        "- Resolve contradictions: if a new fact supersedes an old relationship, use "
        "supersede_relationship. If a node description is outdated, update_entity.\n"
        "- Keep descriptions concise and factual (1-3 sentences).\n"
        "- Use exactly one entity_type from: "
        "Person, Concept, Preference, Fact, Event, Location, Organization, "
        "Project, Technology, Rule, Directive, Role\n"
        "- Use UPPER_SNAKE_CASE for relation names.\n"
        "- When done, stop calling tools and output a brief summary of what was extracted.\n"
    )

    user_prompt = (
        f"Extract entities and relationships from the following conversation excerpt "
        f"and update the knowledge graph accordingly.\n\n"
        f"<conversation>\n{batch_text}\n</conversation>"
    )

    await _agent_loop(llm, system_prompt, user_prompt, tools)


# ---------------------------------------------------------------------------
# Targeted agent
# ---------------------------------------------------------------------------

async def _run_targeted_agent(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    prompt: str,
) -> None:
    """Run a targeted librarian agent for a specific graph-edit task."""
    write_tools = _make_write_tools(conn, write_lock)
    read_tools  = _make_read_tools(conn)
    tools       = write_tools + read_tools

    system_prompt = (
        "You are a targeted knowledge graph editor. Execute the task in the prompt "
        "precisely using the available graph tools. Be surgical — only touch nodes "
        "and edges directly relevant to the task. Do not over-write or modify "
        "unrelated entities. When done, stop calling tools."
    )

    await _agent_loop(llm, system_prompt, prompt, tools)


# ---------------------------------------------------------------------------
# Dedup cycle (not an agent — direct code + one LLM call per pair)
# ---------------------------------------------------------------------------

async def _run_dedup_cycle(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    embedder,
) -> None:
    logger.info("[librarian] dedup cycle starting")
    try:
        from TinyCTX.modules.knowledge.graph import embed_content_for, embed_hash, cosine_similarity, now_ts

        threshold = float(cfg.get("similarity_threshold", 0.85))

        # 1. Fetch all entities
        r = await conn.execute(
            "MATCH (e:Entity) RETURN e.uuid, e.name, e.description, e.entity_type, "
            "e.embed_model, e.embed_hash, e.embedding"
        )
        col_names = r.get_column_names()
        entities = []
        while r.has_next():
            row = r.get_next()
            entities.append(dict(zip(col_names, row)))

        if len(entities) < 2:
            logger.info("[librarian] dedup: fewer than 2 entities, skipping")
            return

        # 2. Refresh stale embeddings
        embed_model_name = getattr(embedder, "model", "")
        stale = []
        for e in entities:
            expected_hash = embed_hash(embed_content_for(e["e.name"], e["e.description"]))
            is_stale = (
                not e["e.embedding"]
                or e["e.embed_model"] != embed_model_name
                or e["e.embed_hash"] != expected_hash
            )
            if is_stale:
                stale.append(e)

        if stale:
            logger.info("[librarian] dedup: refreshing %d stale embedding(s)", len(stale))
            texts   = [embed_content_for(e["e.name"], e["e.description"]) for e in stale]
            vectors = await embedder.embed(texts)
            async with write_lock:
                for e, vec, txt in zip(stale, vectors, texts):
                    h = embed_hash(txt)
                    await conn.execute(
                        "MATCH (e:Entity {uuid: $uid}) "
                        "SET e.embedding = $emb, e.embed_model = $model, "
                        "    e.embed_content = $content, e.embed_hash = $hash",
                        parameters={
                            "uid": e["e.uuid"], "emb": vec,
                            "model": embed_model_name, "content": txt, "hash": h,
                        },
                    )
                    # Sync local copy
                    e["e.embedding"]    = vec
                    e["e.embed_model"]  = embed_model_name
                    e["e.embed_hash"]   = h

        # 3. Find candidate pairs above threshold
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
            logger.info("[librarian] dedup: no candidate pairs above threshold %.2f", threshold)
            return

        logger.info("[librarian] dedup: %d candidate pair(s) to evaluate", len(candidates))

        # Skip pairs already aliased
        already_aliased: set[frozenset] = set()
        r = await conn.execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity) "
            "WHERE r.relation = 'ALIASED_TO' AND r.superseded_at IS NULL "
            "RETURN a.uuid, b.uuid"
        )
        while r.has_next():
            row = r.get_next()
            already_aliased.add(frozenset([row[0], row[1]]))

        # 4. LLM call per pair, sequentially
        for ea, eb, score in candidates:
            pair_key = frozenset([ea["e.uuid"], eb["e.uuid"]])
            if pair_key in already_aliased:
                continue
            await _dedup_pair(conn, write_lock, llm, ea, eb)

        logger.info("[librarian] dedup cycle complete")
    except Exception:
        logger.exception("[librarian] dedup cycle error")


async def _dedup_pair(conn, write_lock: asyncio.Lock, llm, ea: dict, eb: dict) -> None:
    from TinyCTX.modules.knowledge.graph import now_ts

    prompt = (
        "You are comparing two knowledge graph nodes to decide if they represent "
        "the same real-world thing.\n\n"
        f"Node A:\n  uuid: {ea['e.uuid']}\n  name: {ea['e.name']}\n"
        f"  type: {ea['e.entity_type']}\n  description: {ea['e.description']}\n\n"
        f"Node B:\n  uuid: {eb['e.uuid']}\n  name: {eb['e.name']}\n"
        f"  type: {eb['e.entity_type']}\n  description: {eb['e.description']}\n\n"
        "Respond with ONLY a JSON object (no markdown fences):\n"
        "{\n"
        '  "verdict": "duplicate" | "alias" | "distinct",\n'
        '  "canonical_uuid": "<uuid of node to keep — required unless distinct>",\n'
        '  "merged_description": "<consolidated description — required unless distinct>"\n'
        "}\n"
        "duplicate = same real-world entity (merge into one node).\n"
        "alias = different names for the same underlying thing (keep both, add ALIASED_TO edge).\n"
        "distinct = genuinely different entities."
    )

    response_text = ""
    async for event in llm.stream(
        [{"role": "user", "content": prompt}], tools=None
    ):
        from TinyCTX.ai import TextDelta
        if isinstance(event, TextDelta):
            response_text += event.text

    # Parse JSON response
    import re as _re
    raw = response_text.strip()
    # Strip markdown fences if model ignored instructions
    raw = _re.sub(r"^```json?\s*", "", raw)
    raw = _re.sub(r"\s*```$", "", raw)

    try:
        verdict_data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[librarian] dedup: could not parse verdict for %s/%s: %s",
                       ea["e.uuid"][:8], eb["e.uuid"][:8], raw[:200])
        return

    verdict = verdict_data.get("verdict", "distinct")
    canonical_uuid = verdict_data.get("canonical_uuid")
    merged_desc    = verdict_data.get("merged_description", "")

    if verdict == "distinct":
        return

    if not canonical_uuid or canonical_uuid not in {ea["e.uuid"], eb["e.uuid"]}:
        logger.warning("[librarian] dedup: invalid canonical_uuid in verdict")
        return

    dup_uuid = eb["e.uuid"] if canonical_uuid == ea["e.uuid"] else ea["e.uuid"]
    now = now_ts()

    async with write_lock:
        if verdict == "duplicate":
            logger.info("[librarian] dedup: merging %s → %s", dup_uuid[:8], canonical_uuid[:8])
            # Update canonical description
            await conn.execute(
                "MATCH (e:Entity {uuid: $uid}) SET e.description = $desc, e.updated_at = $now, e.embed_hash = ''",
                parameters={"uid": canonical_uuid, "desc": merged_desc, "now": now},
            )
            # Re-home outgoing edges from duplicate
            await conn.execute(
                "MATCH (dup:Entity {uuid: $dup})-[r:Relation]->(x:Entity) "
                "WHERE r.superseded_at IS NULL AND x.uuid <> $canon "
                "CREATE (c:Entity {uuid: $canon})-[:Relation {relation: r.relation, weight: r.weight, description: r.description, created_at: $now, superseded_at: null}]->(x)",
                parameters={"dup": dup_uuid, "canon": canonical_uuid, "now": now},
            )
            # Re-home incoming edges
            await conn.execute(
                "MATCH (x:Entity)-[r:Relation]->(dup:Entity {uuid: $dup}) "
                "WHERE r.superseded_at IS NULL AND x.uuid <> $canon "
                "CREATE (x)-[:Relation {relation: r.relation, weight: r.weight, description: r.description, created_at: $now, superseded_at: null}]->(c:Entity {uuid: $canon})",
                parameters={"dup": dup_uuid, "canon": canonical_uuid, "now": now},
            )
            # Delete duplicate (cascades edges)
            await conn.execute(
                "MATCH (e:Entity {uuid: $uid}) DETACH DELETE e",
                parameters={"uid": dup_uuid},
            )

        elif verdict == "alias":
            logger.info("[librarian] dedup: aliasing %s → %s", dup_uuid[:8], canonical_uuid[:8])
            # Update alias description
            await conn.execute(
                "MATCH (e:Entity {uuid: $uid}) SET e.description = $desc, e.updated_at = $now",
                parameters={"uid": dup_uuid, "desc": merged_desc, "now": now},
            )
            # Add ALIASED_TO edge
            await conn.execute(
                "MATCH (a:Entity {uuid: $alias}), (c:Entity {uuid: $canon}) "
                "CREATE (a)-[:Relation {relation: 'ALIASED_TO', weight: 1.0, description: 'alias', created_at: $now, superseded_at: null}]->(c)",
                parameters={"alias": dup_uuid, "canon": canonical_uuid, "now": now},
            )


# ---------------------------------------------------------------------------
# Graph write tools (shared by buffer and targeted agents)
# ---------------------------------------------------------------------------

def _make_write_tools(conn, write_lock: asyncio.Lock) -> list[dict]:
    """Build write tool definitions as dicts for the simple agent loop."""
    from TinyCTX.modules.knowledge.graph import new_uuid, now_ts, embed_content_for

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
        # Check existing
        r = await conn.execute(
            "MATCH (e:Entity) WHERE e.name = $name AND e.entity_type = $et RETURN e.uuid LIMIT 1",
            parameters={"name": name, "et": entity_type},
        )
        if r.has_next():
            uid = r.get_next()[0]
            async with write_lock:
                await conn.execute(
                    "MATCH (e:Entity {uuid: $uid}) "
                    "SET e.description = $desc, e.updated_at = $now, "
                    "    e.priority = $pri, e.pinned = $pin, e.embed_hash = ''",
                    parameters={"uid": uid, "desc": description, "now": now, "pri": priority, "pin": pinned},
                )
            return uid

        uid = new_uuid()
        async with write_lock:
            await conn.execute(
                "CREATE (e:Entity {uuid: $uid, name: $name, entity_type: $et, "
                "description: $desc, pinned: $pin, priority: $pri, mention_count: 0, "
                "created_at: $now, updated_at: $now, embed_model: '', "
                "embed_content: '', embed_hash: '', embedding: []})",
                parameters={
                    "uid": uid, "name": name, "et": entity_type,
                    "desc": description, "pin": pinned, "pri": priority, "now": now,
                },
            )
        return uid

    async def update_entity(
        uuid: str,
        description: str | None = None,
        priority: int | None = None,
        pinned: bool | None = None,
    ) -> str:
        """
        Update fields on an existing entity. Only provided fields are changed.
        Marks embedding as stale (embed_hash cleared) when description changes.

        Args:
            uuid: The entity UUID (from add_entity or find_entity).
            description: New description (optional).
            priority: New priority value (optional).
            pinned: New pinned flag (optional).
        """
        now = now_ts()
        sets = ["e.updated_at = $now"]
        params: dict = {"uid": uuid, "now": now}
        if description is not None:
            sets.append("e.description = $desc")
            sets.append("e.embed_hash = ''")
            params["desc"] = description
        if priority is not None:
            sets.append("e.priority = $pri")
            params["pri"] = priority
        if pinned is not None:
            sets.append("e.pinned = $pin")
            params["pin"] = pinned
        if len(sets) == 1:
            return f"[no fields to update for {uuid}]"
        async with write_lock:
            await conn.execute(
                f"MATCH (e:Entity {{uuid: $uid}}) SET {', '.join(sets)}",
                parameters=params,
            )
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
            relation: UPPER_SNAKE_CASE relation label (e.g. USES, KNOWS, CREATED).
            weight: Strength of the relationship, 0.0-1.0 (default 0.5).
            description: Optional human-readable explanation.
        """
        now = now_ts()
        async with write_lock:
            await conn.execute(
                "MATCH (a:Entity {uuid: $src}), (b:Entity {uuid: $tgt}) "
                "CREATE (a)-[:Relation {relation: $rel, weight: $w, "
                "description: $desc, created_at: $now, superseded_at: null}]->(b)",
                parameters={
                    "src": source_uuid, "tgt": target_uuid, "rel": relation.upper(),
                    "w": weight, "desc": description, "now": now,
                },
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
        The old edge is preserved (superseded_at set) for audit purposes.

        Args:
            src_uuid: Source entity UUID.
            tgt_uuid: Target entity UUID.
            old_relation: The relation label to supersede.
            new_relation: The new relation label to create.
            weight: Weight for the new relationship.
            description: Optional explanation for the new relationship.
        """
        now = now_ts()
        async with write_lock:
            await conn.execute(
                "MATCH (a:Entity {uuid: $src})-[r:Relation]->(b:Entity {uuid: $tgt}) "
                "WHERE r.relation = $old AND r.superseded_at IS NULL "
                "SET r.superseded_at = $now",
                parameters={"src": src_uuid, "tgt": tgt_uuid, "old": old_relation.upper(), "now": now},
            )
            await conn.execute(
                "MATCH (a:Entity {uuid: $src}), (b:Entity {uuid: $tgt}) "
                "CREATE (a)-[:Relation {relation: $rel, weight: $w, "
                "description: $desc, created_at: $now, superseded_at: null}]->(b)",
                parameters={
                    "src": src_uuid, "tgt": tgt_uuid, "rel": new_relation.upper(),
                    "w": weight, "desc": description, "now": now,
                },
            )
        return f"superseded {old_relation} → {new_relation} from {src_uuid[:8]} → {tgt_uuid[:8]}"

    async def delete_entity(uuid: str) -> str:
        """
        Hard-delete an entity and all its edges. Use sparingly — only for
        entirely erroneous nodes (errors of fact, duplicates, test data).

        Args:
            uuid: The entity UUID to delete.
        """
        async with write_lock:
            await conn.execute(
                "MATCH (e:Entity {uuid: $uid}) DETACH DELETE e",
                parameters={"uid": uuid},
            )
        return f"deleted entity {uuid[:8]}"

    async def delete_relationship(
        src_uuid: str,
        tgt_uuid: str,
        relation: str,
    ) -> str:
        """
        Delete all active edges of a given relation type between two entities.

        Args:
            src_uuid: Source entity UUID.
            tgt_uuid: Target entity UUID.
            relation: The relation label to delete.
        """
        async with write_lock:
            await conn.execute(
                "MATCH (a:Entity {uuid: $src})-[r:Relation]->(b:Entity {uuid: $tgt}) "
                "WHERE r.relation = $rel AND r.superseded_at IS NULL DELETE r",
                parameters={"src": src_uuid, "tgt": tgt_uuid, "rel": relation.upper()},
            )
        return f"deleted {relation} from {src_uuid[:8]} → {tgt_uuid[:8]}"

    for fn in [
        add_entity, update_entity, add_relationship,
        supersede_relationship, delete_entity, delete_relationship,
    ]:
        tools.append({"fn": fn, "name": fn.__name__, "doc": fn.__doc__})

    return tools


def _make_read_tools(conn) -> list[dict]:
    """Build read tool definitions for librarian agents."""

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
        row = r.get_next()
        cols = r.get_column_names()
        data = dict(zip(cols, row))
        # Omit embedding blob from text output
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

        out_lines = []
        while edges_out.has_next():
            row = edges_out.get_next()
            out_lines.append(f"  →[{row[2]}]→ {row[1]} ({row[0][:8]}) weight={row[3]}")
        in_lines = []
        while edges_in.has_next():
            row = edges_in.get_next()
            in_lines.append(f"  ←[{row[2]}]← {row[1]} ({row[0][:8]}) weight={row[3]}")

        lines = [f"Entity: {data.get('e.name')} [{data.get('e.entity_type')}]"]
        lines.append(f"uuid: {uuid}")
        lines.append(f"description: {data.get('e.description')}")
        lines.append(f"pinned: {data.get('e.pinned')}  priority: {data.get('e.priority')}")
        if out_lines:
            lines.append("outgoing:")
            lines.extend(out_lines)
        if in_lines:
            lines.append("incoming:")
            lines.extend(in_lines)
        return "\n".join(lines)

    tools = []
    for fn in [find_entity, get_entity]:
        tools.append({"fn": fn, "name": fn.__name__, "doc": fn.__doc__})
    return tools


# ---------------------------------------------------------------------------
# Simple agent loop for librarian agents
# ---------------------------------------------------------------------------

async def _agent_loop(
    llm,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    max_cycles: int = 20,
) -> None:
    """
    Minimal agent loop for librarian agents.
    No Lane, no DB, no streaming output — just run to completion.
    tools is a list of {"fn": coroutine_fn, "name": str, "doc": str}.
    """
    from TinyCTX.ai import TextDelta, ToolCallAssembled, LLMError

    # Build OpenAI-compat tool definitions from docstrings
    tool_defs = []
    tool_map  = {}
    for t in tools:
        import inspect
        sig = inspect.signature(t["fn"])
        props: dict = {}
        required: list = []
        for pname, param in sig.parameters.items():
            ptype = "string"
            ann = param.annotation
            if ann in (int,):
                ptype = "integer"
            elif ann in (float,):
                ptype = "number"
            elif ann in (bool,):
                ptype = "boolean"
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
        {"role": "system",  "content": system_prompt},
        {"role": "user",    "content": user_prompt},
    ]

    for cycle in range(max_cycles):
        text_chunks: list[str] = []
        tool_calls:  list[dict] = []

        async for event in llm.stream(messages, tools=tool_defs):
            if isinstance(event, TextDelta):
                text_chunks.append(event.text)
            elif isinstance(event, ToolCallAssembled):
                tool_calls.append({
                    "id": event.call_id, "name": event.tool_name, "args": event.args
                })
            elif isinstance(event, LLMError):
                logger.error("[librarian/agent] LLM error: %s", event.message)
                return

        response_text = "".join(text_chunks)

        if not tool_calls:
            # Done
            if response_text:
                logger.debug("[librarian/agent] completed: %s", response_text[:120])
            return

        # Append assistant turn with tool calls
        messages.append({
            "role": "assistant",
            "content": response_text,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name":      tc["name"],
                        "arguments": json.dumps(tc["args"]),
                    },
                }
                for tc in tool_calls
            ],
        })

        # Execute each tool call
        for tc in tool_calls:
            fn = tool_map.get(tc["name"])
            if fn is None:
                result = f"[unknown tool: {tc['name']}]"
            else:
                try:
                    result = await fn(**tc["args"])
                except Exception as exc:
                    result = f"[error: {exc}]"
                    logger.warning("[librarian/agent] tool %s error: %s", tc["name"], exc)

            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      str(result),
            })

    logger.warning("[librarian/agent] hit max_cycles (%d)", max_cycles)


# ---------------------------------------------------------------------------
# Entry point guard
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()

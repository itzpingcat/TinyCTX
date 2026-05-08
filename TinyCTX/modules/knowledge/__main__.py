"""
modules/knowledge/__main__.py

Registers the knowledge module into the agent.

On load:
  1. Starts the librarian sidecar process
  2. Registers a HOOK_POST_TURN buffer writer
  3. Registers read tools on the main agent (kg_search, kg_traverse,
     kg_get_entity, kg_list, kg_stats)
  4. Registers call_librarian (always-on)
  5. Registers a PromptProvider for pinned entity injection

The librarian process is the sole writer to the KùzuDB graph.
The main agent tools are read-only.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def register(agent) -> None:
    # Normalise: accept Runtime or legacy AgentLoop.
    from TinyCTX.runtime import Runtime as _Runtime
    _rt = agent if isinstance(agent, _Runtime) else None
    if _rt is not None:
        class _Shim:
            config       = _rt.config
            context      = _rt.context
            tool_handler = _rt.tool_handler
            def register_background_hook(self, fn): _rt.register_background_hook(fn)
        agent = _Shim()
    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------
    try:
        from TinyCTX.modules.knowledge import EXTENSION_META
        defaults: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        defaults = {}

    overrides: dict = {}
    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        overrides = agent.config.extra.get("knowledge", {})

    cfg: dict = {**defaults, **overrides}

    def _resolve(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else workspace / p

    graph_path    = _resolve(cfg["graph_path"])
    libbuffer_dir = _resolve(cfg["libbuffer_dir"])
    sock_path     = _resolve(cfg["ipc_socket"])
    pinned_prio   = int(cfg.get("pinned_priority", 5))

    graph_path.parent.mkdir(parents=True, exist_ok=True)
    libbuffer_dir.mkdir(parents=True, exist_ok=True)
    sock_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Open read-only graph handle for main agent tools
    # ------------------------------------------------------------------
    from TinyCTX.modules.knowledge.graph import GraphDB
    graph_db = GraphDB(graph_path)
    atexit.register(graph_db.close)

    # ------------------------------------------------------------------
    # Embedder for kg_search semantic mode
    # ------------------------------------------------------------------
    embedder        = None
    embedding_model = cfg.get("embedding_model", "").strip()
    if embedding_model:
        try:
            from TinyCTX.ai import Embedder
            emb_cfg  = agent.config.get_embedding_model(embedding_model)
            embedder = Embedder.from_config(emb_cfg)
            logger.info("[knowledge] embedder: %s @ %s", emb_cfg.model, emb_cfg.base_url)
        except (KeyError, ValueError) as exc:
            logger.warning(
                "[knowledge] embedding_model '%s' not usable (%s) — semantic search disabled",
                embedding_model, exc,
            )

    # ------------------------------------------------------------------
    # 1. Start librarian sidecar process
    # ------------------------------------------------------------------
    _start_librarian(cfg, agent, workspace, graph_path, libbuffer_dir, sock_path)

    # ------------------------------------------------------------------
    # 2. HOOK_POST_TURN — buffer writer
    # ------------------------------------------------------------------
    from TinyCTX.modules.knowledge.buffer import SessionBuffer

    # Determine bridge name from context state (populated at intake)
    # Falls back to "cli" if not running under a bridge.
    bridge_name = "cli"
    session_buffer = SessionBuffer(libbuffer_dir, bridge_name)

    async def _post_turn_hook(tail_node_id: str) -> None:
        # Read the last two turns from the DB directly using tail_node_id.
        # This avoids depending on agent.context which doesn't exist on Runtime.
        db = _rt.db if _rt is not None else getattr(agent, '_db', None)
        if db is None:
            return

        nodes = db.get_ancestors(tail_node_id)
        if not nodes:
            return

        # Replay session state for author_name
        from TinyCTX.context import Context
        ctx = Context()
        ctx.set_db(db)
        ctx.set_tail(tail_node_id)
        state, _ = ctx._load_state_from_db()
        user_name = state.get("author_name") or "user"

        user_text     = ""
        asst_text     = ""
        attachment_names: list[str] = []

        for node in reversed(nodes):
            if node.role == "assistant" and not asst_text:
                asst_text = node.content if isinstance(node.content, str) else ""
            elif node.role == "user" and not user_text:
                content = node.content
                import json as _json
                if content.startswith("["):
                    try:
                        blocks = _json.loads(content)
                        for block in blocks:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    user_text += block.get("text", "")
                                elif block.get("type") in ("image_url", "image", "document"):
                                    attachment_names.append(block.get("type", "attachment"))
                    except Exception:
                        user_text = content
                else:
                    user_text = content
            if user_text and asst_text:
                break

        if user_text or asst_text:
            session_buffer.append_turn(
                username=user_name,
                user_text=user_text.strip(),
                assistant_text=asst_text.strip(),
                attachment_names=attachment_names or None,
            )

    agent.register_background_hook(_post_turn_hook)

    # ------------------------------------------------------------------
    # 3. Read tools
    # ------------------------------------------------------------------

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
        from TinyCTX.modules.knowledge.graph import top_k_cosine

        if semantic and embedder is not None:
            try:
                query_vec = await embedder.embed_one(query)
            except Exception as exc:
                logger.warning("[knowledge] kg_search embed failed: %s — falling back to keyword", exc)
                query_vec = None
        else:
            query_vec = None

        if query_vec is not None:
            # Cosine similarity over all stored embeddings
            all_embs = graph_db.all_entities_with_embeddings()
            top      = top_k_cosine(query_vec, all_embs, top_k)
            uids     = [uid for uid, _ in top]
        else:
            # Keyword fallback
            results = graph_db.find_entity(name=query)
            uids = [r["uuid"] for r in results[:top_k]]

        if not uids:
            return "[no matching entities found]"

        graph_db.bump_mention_count(uids)

        lines = []
        for uid in uids:
            entity = graph_db.get_entity(uid)
            if not entity:
                continue
            name  = entity.get("e.name", "?")
            etype = entity.get("e.entity_type", "?")
            desc  = entity.get("e.description", "")
            lines.append(f"[{etype}] {name} (uuid: {uid[:8]})\n  {desc}")
            for edge in entity.get("edges_out", []):
                lines.append(f"  →[{edge['relation']}]→ {edge['target_name']}")
            for edge in entity.get("edges_in", []):
                lines.append(f"  ←[{edge['relation']}]← {edge['source_name']}")

        return "\n\n".join(lines) if lines else "[no entities found]"

    async def kg_traverse(
        uuid: str,
        hops: int = 1,
        relation_filter: str = "",
    ) -> str:
        """
        Walk the graph from an entity outward up to N hops.
        Returns all active edges encountered.

        Args:
            uuid: Starting entity UUID.
            hops: Number of hops to traverse (default 1, max 3).
            relation_filter: If provided, only follow edges with this relation label.
        """
        hops = min(int(hops), 3)
        edges = graph_db.traverse(uuid, hops, relation_filter or None)
        if not edges:
            return f"[no edges found from {uuid[:8]}]"
        lines = [f"Traversal from {uuid[:8]} ({hops} hop(s)):"]
        for e in edges:
            lines.append(f"  {e['source_uuid'][:8] if 'source_uuid' in e else uuid[:8]} "
                         f"→[{e['relation']}]→ {e['target_name']} ({e['target_uuid'][:8]})")
        return "\n".join(lines)

    async def kg_get_entity(uuid: str) -> str:
        """
        Retrieve full details of a knowledge graph entity including all
        active incoming and outgoing relationships.

        Args:
            uuid: The entity UUID to retrieve.
        """
        entity = graph_db.get_entity(uuid)
        if not entity:
            return f"[entity {uuid[:8]} not found]"

        name  = entity.get("e.name", "?")
        etype = entity.get("e.entity_type", "?")
        desc  = entity.get("e.description", "")
        pin   = entity.get("e.pinned", False)
        pri   = entity.get("e.priority", 40)
        mc    = entity.get("e.mention_count", 0)

        lines = [
            f"[{etype}] {name}",
            f"uuid: {uuid}",
            f"description: {desc}",
            f"pinned: {pin}  priority: {pri}  mentions: {mc}",
        ]
        for e in entity.get("edges_out", []):
            lines.append(f"  →[{e['relation']}]→ {e['target_name']} ({e['target_uuid'][:8]})"
                         + (f" — {e['description']}" if e.get("description") else ""))
        for e in entity.get("edges_in", []):
            lines.append(f"  ←[{e['relation']}]← {e['source_name']} ({e['source_uuid'][:8]})"
                         + (f" — {e['description']}" if e.get("description") else ""))

        return "\n".join(lines)

    async def kg_list(entity_type: str = "", pinned_only: bool = False) -> str:
        """
        List knowledge graph entities, optionally filtered by type or pinned status.

        Args:
            entity_type: Filter by type (e.g. Person, Project, Technology). Empty = all types.
            pinned_only: If true, return only pinned entities.
        """
        entities = graph_db.list_entities(
            entity_type=entity_type or None,
            pinned_only=pinned_only,
        )
        if not entities:
            return "[no entities found]"
        lines = []
        for e in entities:
            pin = "📌 " if e.get("pinned") else ""
            lines.append(f"{pin}[{e['entity_type']}] {e['name']} ({e['uuid'][:8]}) pri={e['priority']}\n  {e['description']}")
        return "\n\n".join(lines)

    async def kg_stats() -> str:
        """
        Show knowledge graph statistics: entity count, edge count, breakdown by type.
        """
        stats = graph_db.get_stats()
        lines = [
            f"Entities: {stats['entity_count']}",
            f"Active edges: {stats['active_edge_count']}",
            "By type:",
        ]
        for etype, count in stats["by_type"].items():
            lines.append(f"  {etype}: {count}")
        return "\n".join(lines)

    for fn in [kg_search, kg_traverse, kg_get_entity, kg_list, kg_stats]:
        vis = fn.__name__
        # kg_search always-on; others deferred (BM25-discoverable via tools_search)
        always = (vis == "kg_search")
        agent.tool_handler.register_tool(fn, always_on=always, min_permission=25)

    # ------------------------------------------------------------------
    # 4. call_librarian (always-on)
    # ------------------------------------------------------------------

    async def call_librarian(prompt: str = "") -> str:
        """
        Signal the librarian process to update the knowledge graph.

        With no prompt: trigger normal buffer ingest immediately (processes
        any pending conversation history in libbuffer/).

        With a prompt: spawn a targeted agent to execute that specific
        graph-edit instruction (e.g. "remember that Kamie prefers async Python",
        "update the TinyCTX project description", "link TinyCTX to Python").

        Args:
            prompt: Optional instruction for the targeted librarian agent.
                Leave empty to trigger buffer ingest.
        """
        from TinyCTX.modules.knowledge.ipc import send_ipc, IPCError

        if prompt.strip():
            msg = {"type": "targeted", "prompt": prompt.strip()}
        else:
            msg = {"type": "trigger"}

        try:
            await send_ipc(sock_path, msg)
            if prompt.strip():
                return f"[librarian: targeted agent queued — '{prompt[:60]}']"
            return "[librarian: buffer ingest triggered]"
        except IPCError as exc:
            logger.warning("[knowledge] call_librarian IPC failed: %s", exc)
            return f"[librarian: could not reach librarian process — {exc}]"

    agent.tool_handler.register_tool(call_librarian, always_on=True, min_permission=25)

    # ------------------------------------------------------------------
    # 5. Pinned entity PromptProvider
    # ------------------------------------------------------------------

    def _pinned_provider(_ctx) -> str | None:
        entities = graph_db.get_pinned_entities()
        if not entities:
            return None
        lines = ["--- Knowledge Graph (pinned) ---"]
        for e in entities:
            lines.append(f"[{e['entity_type']}] {e['name']} — {e['description']}")
        lines.append("--------------------------------")
        return "\n".join(lines)

    agent.context.register_prompt(
        "knowledge_pinned",
        _pinned_provider,
        role="system",
        priority=pinned_prio,
    )

    logger.info(
        "[knowledge] ready — graph: %s | libbuffer: %s | embedder: %s",
        graph_path, libbuffer_dir, embedding_model or "none",
    )


# ---------------------------------------------------------------------------
# Librarian sidecar launcher
# ---------------------------------------------------------------------------

def _start_librarian(
    cfg: dict,
    agent,
    workspace: Path,
    graph_path: Path,
    libbuffer_dir: Path,
    sock_path: Path,
) -> None:
    """
    Launch the librarian process as a subprocess. Checks PID file first to
    avoid duplicate launches. Registers atexit cleanup.
    """
    pid_file = workspace / "knowledge" / "librarian.pid"

    # Check if already running
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)  # raises ProcessLookupError if dead
            logger.info("[knowledge] librarian already running (PID %d)", old_pid)
            return
        except (ProcessLookupError, ValueError, PermissionError):
            pass  # stale PID
        # Clean up stale socket
        if sock_path.exists():
            try:
                sock_path.unlink()
            except OSError:
                pass

    # Build config dict to pass to librarian process
    primary_name = agent.config.llm.primary
    primary_mc   = agent.config.models.get(primary_name)
    try:
        api_key = primary_mc.api_key if primary_mc else ""
    except EnvironmentError:
        api_key = ""

    embed_cfg: dict = {}
    embedding_model = cfg.get("embedding_model", "").strip()
    if embedding_model and embedding_model in agent.config.models:
        em = agent.config.models[embedding_model]
        try:
            emb_key = em.api_key
        except EnvironmentError:
            emb_key = ""
        embed_cfg = {
            "model":    em.model,
            "base_url": em.base_url,
            "api_key":  emb_key,
        }

    librarian_cfg = {
        "workspace":             str(workspace),
        "graph_path":            str(graph_path.relative_to(workspace)),
        "libbuffer_dir":         str(libbuffer_dir.relative_to(workspace)),
        "ipc_socket":            str(sock_path.relative_to(workspace)),
        "log_level":             agent.config.logging.level,
        "trigger_file_size_kb":  cfg.get("trigger_file_size_kb", 64),
        "trigger_interval_hours": cfg.get("trigger_interval_hours", 6),
        "batch_size":            cfg.get("batch_size", 20),
        "max_concurrent":        cfg.get("max_concurrent", 4),
        "dedup_enabled":         cfg.get("dedup_enabled", True),
        "dedup_interval_hours":  cfg.get("dedup_interval_hours", 24),
        "similarity_threshold":  cfg.get("similarity_threshold", 0.85),
        "primary_model": {
            "model":      primary_mc.model if primary_mc else "",
            "base_url":   primary_mc.base_url if primary_mc else "",
            "api_key":    api_key,
            "max_tokens": primary_mc.max_tokens if primary_mc else 2048,
            "temperature": primary_mc.temperature if primary_mc else 0.7,
        },
        "embed_model": embed_cfg,
    }

    librarian_script = Path(__file__).parent / "librarian_process.py"
    log_file_path    = workspace / "knowledge" / "librarian.log"

    import subprocess
    log_fh = open(log_file_path, "a", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(librarian_script), json.dumps(librarian_cfg)],
        stdout=log_fh,
        stderr=log_fh,
        close_fds=True,
    )
    logger.info("[knowledge] librarian process started (PID %d)", proc.pid)

    def _cleanup() -> None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            log_fh.close()
        except Exception:
            pass

    import atexit
    atexit.register(_cleanup)

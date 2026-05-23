"""
modules/memory/__main__.py

Registers the memory module.

register_runtime(runtime) — called once at startup:
  1. Resolves config, builds embedder, LLM, ConversationDB
  2. Builds LibrarianRunner and starts the poll loop
  3. Inits tools module globals (write_conn, graph_db, embedder)
  4. Sets module-level singletons for register_agent to consume

register_agent(cycle) — called per AgentCycle after tool_handler is ready:
  1. Registers kg_* read tools + call_librarian on cycle.tool_handler
  2. Registers pinned entity PromptProvider on cycle.context

The LibrarianRunner owns the single ladybug.Database object and is the sole
writer to the graph. All connections (reader and writer) are created from
that one Database object, satisfying Ladybug's one-READ_WRITE-Database rule.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

# Module-level singletons set by register_runtime.
_runner:       "LibrarianRunner | None" = None
_workspace:    Path | None = None
_graph_db:     object | None = None
_tools:        "ModuleType | None" = None
_pinned_prio:  int = 5
_token_budget: int = 4096


# ---------------------------------------------------------------------------
# LibrarianRunner — in-process background task
# ---------------------------------------------------------------------------

class LibrarianRunner:
    """
    Owns the single ladybug.Database, vends connections, and runs the
    librarian poll loop as an asyncio background task.

    All graph writes go through the async_conn + write_lock held here.
    Read connections (for GraphDB / main-agent tools) are also created
    from the same db object.
    """

    def __init__(
        self,
        cfg: dict,
        graph_path: Path,
        log_path: Path,
        conv_db,
        llm,
        embedder,
    ) -> None:
        import ladybug
        from TinyCTX.modules.memory.graph import init_schema

        graph_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._db = ladybug.Database(str(graph_path))
        except Exception as exc:
            logger.warning(
                "[memory] graph DB failed to open (%s) — wiping corrupted files and retrying",
                exc,
            )
            # Wipe all ladybug/kuzu auxiliary files next to the DB.
            parent = graph_path.parent
            stem   = graph_path.name
            for p in parent.iterdir():
                if p.name.startswith(stem) and p.name != stem:
                    p.unlink()
                    logger.info("[memory] deleted %s", p)
            self._db = ladybug.Database(str(graph_path))

        self._write_conn = ladybug.AsyncConnection(
            self._db,
            max_concurrent_queries=int(cfg.get("max_concurrent", 4)),
        )
        self._write_lock = asyncio.Lock()

        # Initialise schema synchronously on a plain connection
        sync_conn = ladybug.Connection(self._db)
        init_schema(sync_conn)
        sync_conn.close()

        self._cfg      = cfg
        self._conv_db  = conv_db
        self._llm      = llm
        self._embedder = embedder

        # Dedicated file logger for all librarian agent text output
        self.agent_logger = logging.getLogger("memory.librarian.agent")
        if not self.agent_logger.handlers:
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            self.agent_logger.addHandler(fh)
            self.agent_logger.setLevel(logging.DEBUG)
            self.agent_logger.propagate = False

        # Queue replaces IPC socket: call_librarian puts messages here
        self.queue: asyncio.Queue = asyncio.Queue()

        self._task: asyncio.Task | None = None
        self._state = {
            "last_poll_ts":  0.0,
            "last_dedup_ts": 0.0,
            "dedup_running": False,
        }
        self._active_tasks: set[asyncio.Task] = set()

    def new_read_connection(self):
        """Return a new sync Connection from the shared Database for read tools."""
        import ladybug
        return ladybug.Connection(self._db)

    def start(self) -> None:
        """Schedule the poll loop as a background asyncio task."""
        self._task = asyncio.create_task(self._run(), name="knowledge-librarian")
        logger.info("[memory] LibrarianRunner started")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._poll_cycle()
                except Exception:
                    logger.exception("[memory/librarian] poll cycle error")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            if self._active_tasks:
                logger.info(
                    "[memory/librarian] draining %d in-flight task(s)",
                    len(self._active_tasks),
                )
                await asyncio.gather(*self._active_tasks, return_exceptions=True)
            logger.info("[memory/librarian] stopped")

    async def _poll_cycle(self) -> None:
        from TinyCTX.modules.memory.librarian_agents import (
            run_buffer_agent, run_targeted_agent, run_dedup_cycle,
            nodes_to_text,
        )

        # Reap finished tasks
        done = {t for t in self._active_tasks if t.done()}
        for t in done:
            if not t.cancelled() and t.exception():
                logger.error("[memory/librarian] agent task raised: %s", t.exception())
        self._active_tasks -= done

        max_concurrent = int(self._cfg.get("max_concurrent", 4))
        batch_size     = int(self._cfg.get("batch_size", 20))

        # Drain queue messages
        while not self.queue.empty():
            msg = self.queue.get_nowait()
            msg_type = msg.get("type")
            if msg_type == "targeted":
                prompt = msg.get("prompt", "").strip()
                if prompt and len(self._active_tasks) < max_concurrent:
                    t = asyncio.create_task(
                        run_targeted_agent(
                            self._cfg, self._write_conn, self._write_lock,
                            self._llm, prompt, self.agent_logger,
                        )
                    )
                    self._active_tasks.add(t)
                elif prompt:
                    logger.warning("[memory/librarian] concurrency cap reached, dropping targeted msg")

        # Node walk on schedule
        now = time.time()
        interval_secs = float(self._cfg.get("trigger_interval_hours", 6)) * 3600
        if (now - self._state["last_poll_ts"]) >= interval_secs:
            self._state["last_poll_ts"] = now

            async with self._write_lock:
                tail_nodes = self._conv_db.get_tail_nodes()
                batches: list[tuple[list, str, str]] = []
                for tail in tail_nodes:
                    if len(self._active_tasks) + len(batches) >= max_concurrent:
                        break
                    flagged_ids = self._conv_db.flag_branch(tail.id, "librarian_visited")
                    if not flagged_ids:
                        continue
                    batch_text, agent_name = nodes_to_text(self._conv_db, list(reversed(flagged_ids)), batch_size)
                    batches.append((flagged_ids, batch_text, agent_name))

            for flagged_ids, batch_text, agent_name in batches:
                if not batch_text.strip():
                    continue
                t = asyncio.create_task(
                    run_buffer_agent(
                        self._cfg, self._write_conn, self._write_lock,
                        self._llm, batch_text, agent_name, self.agent_logger,
                    )
                )
                self._active_tasks.add(t)
                logger.info("[memory/librarian] dispatched agent for %d node(s)", len(flagged_ids))

        # Dedup on schedule
        dedup_enabled  = bool(self._cfg.get("dedup_enabled", True))
        dedup_interval = float(self._cfg.get("dedup_interval_hours", 24)) * 3600
        if (
            dedup_enabled
            and not self._state["dedup_running"]
            and (now - self._state["last_dedup_ts"]) >= dedup_interval
            and self._embedder is not None
            and len(self._active_tasks) < max_concurrent
        ):
            self._state["dedup_running"] = True
            self._state["last_dedup_ts"] = now
            t = asyncio.create_task(
                run_dedup_cycle(
                    self._cfg, self._write_conn, self._write_lock,
                    self._llm, self._embedder, self.agent_logger,
                )
            )
            t.add_done_callback(lambda _: self._state.__setitem__("dedup_running", False))
            self._active_tasks.add(t)


# ---------------------------------------------------------------------------
# call_librarian — defined at module level, reads globals set by register_runtime
# ---------------------------------------------------------------------------

async def call_librarian(prompt: str = "", file_path: str = "") -> str:
    """
    Signal the librarian to update the knowledge graph.

    With no arguments: trigger normal conversation node ingest immediately.

    With prompt only: spawn a targeted agent for that specific graph-edit
    instruction (e.g. "remember that Kamie prefers async Python").

    With file_path: read that file and ingest its contents into the graph.
    One file per call. Combine with prompt for extra instructions
    (e.g. file_path="notes.md", prompt="focus on the people mentioned").

    Args:
        prompt: Optional instruction for the targeted librarian agent.
        file_path: Optional path to a plain-text or markdown file to ingest.
            Absolute, or relative to the workspace root.
    """
    if file_path.strip():
        assert _workspace is not None
        assert _runner is not None
        p = Path(file_path.strip())
        if not p.is_absolute():
            p = _workspace / p
        try:
            file_text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[librarian: could not read '{file_path}': {exc}]"
        combined = f"<file name=\"{p.name}\">\n{file_text}\n</file>"
        if prompt.strip():
            combined = f"{combined}\n\n{prompt.strip()}"
        _runner.queue.put_nowait({"type": "targeted", "prompt": combined})
        return f"[librarian: file agent queued — '{p.name}']"
    elif prompt.strip():
        assert _runner is not None
        _runner.queue.put_nowait({"type": "targeted", "prompt": prompt.strip()})
        return f"[librarian: targeted agent queued — '{prompt[:60]}']"
    else:
        assert _runner is not None
        _runner.queue.put_nowait({"type": "trigger"})
        return "[librarian: node ingest triggered]"


# ---------------------------------------------------------------------------
# register_runtime — one-time startup
# ---------------------------------------------------------------------------

def register_runtime(runtime) -> None:
    global _runner, _workspace, _graph_db, _tools, _pinned_prio, _token_budget

    _workspace = Path(runtime.config.workspace.path).expanduser().resolve()
    _workspace.mkdir(parents=True, exist_ok=True)

    # Config
    try:
        from TinyCTX.modules.memory import EXTENSION_META
        defaults: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        defaults = {}

    overrides: dict = {}
    if hasattr(runtime.config, "extra") and isinstance(runtime.config.extra, dict):
        overrides = runtime.config.extra.get("memory", {})

    cfg: dict = {**defaults, **overrides}

    ws = _workspace  # local binding so pylance knows it's non-None here
    assert ws is not None

    def _resolve(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ws / p

    graph_path = _resolve(cfg["graph_path"])
    log_path   = _resolve(cfg.get("librarian_log", "memory/librarian.log"))
    agent_db   = ws / "agent.db"

    _pinned_prio  = int(cfg.get("pinned_priority", 5))
    _token_budget = int(cfg.get("memory_block_tokens", 4096))

    # Embedder
    embedder        = None
    embedding_model = cfg.get("embedding_model", "").strip()
    if embedding_model:
        try:
            from TinyCTX.ai import Embedder
            emb_cfg  = runtime.config.get_embedding_model(embedding_model)
            embedder = Embedder.from_config(emb_cfg)
            logger.info("[memory] embedder: %s @ %s", emb_cfg.model, emb_cfg.base_url)
        except (KeyError, ValueError) as exc:
            logger.warning(
                "[memory] embedding_model '%s' not usable (%s) — semantic search disabled",
                embedding_model, exc,
            )

    # LLM for librarian agents
    primary_name        = runtime.config.llm.primary
    librarian_model_key = cfg.get("librarian_model", "").strip() or primary_name
    primary_mc          = runtime.config.models.get(librarian_model_key)
    try:
        api_key = primary_mc.api_key if primary_mc else ""
    except EnvironmentError:
        api_key = ""

    from TinyCTX.ai import LLM
    llm = LLM(
        base_url=primary_mc.base_url if primary_mc else "",
        api_key=api_key,
        model=primary_mc.model if primary_mc else "",
        max_tokens=primary_mc.max_tokens if primary_mc else 2048,
        temperature=primary_mc.temperature if primary_mc else 0.7,
    )

    # ConversationDB
    from TinyCTX.db import ConversationDB
    conv_db = ConversationDB(agent_db)
    atexit.register(conv_db.close)

    # LibrarianRunner
    _runner = LibrarianRunner(cfg, graph_path, log_path, conv_db, llm, embedder)
    atexit.register(_runner.stop)

    # GraphDB
    from TinyCTX.modules.memory.graph import GraphDB
    import TinyCTX.modules.memory.tools as tools_mod

    read_conn = _runner.new_read_connection()
    _graph_db = GraphDB(read_conn)
    atexit.register(read_conn.close)

    _tools = tools_mod
    _tools.init(_runner._write_conn, _runner._write_lock, _graph_db, embedder)

    _runner.start()

    logger.info(
        "[memory] ready — graph: %s | embedder: %s",
        graph_path, embedding_model or "none",
    )


# ---------------------------------------------------------------------------
# register_agent — per AgentCycle
# ---------------------------------------------------------------------------

def register_agent(cycle) -> None:
    if _runner is None:
        logger.error("[memory] register_agent called before register_runtime — skipping")
        return

    assert _tools is not None
    for fn in [
        _tools.kg_search,
        _tools.kg_traverse,
        _tools.kg_get_entity,
        _tools.kg_list,
        _tools.kg_stats,
    ]:
        always = (fn.__name__ == "kg_search")
        cycle.tool_handler.register_tool(fn, always_on=always, min_permission=25)

    cycle.tool_handler.register_tool(call_librarian, always_on=True, min_permission=25)

    def _build_memory_block(gdb, budget: int) -> str | None:
        pinned = gdb.get_pinned_entities_full()
        if not pinned:
            return None

        def _get(e: dict, field: str) -> str:
            return str(e.get(f"e.{field}", e.get(field, "")) or "")

        pinned_uuids: set[str] = {_get(e, "uuid") for e in pinned}

        def _render_pinned(e: dict) -> str:
            lines = [f"[{_get(e, 'entity_type')}] {_get(e, 'name')} — {_get(e, 'description')}"]
            for edge in e.get("edges_out", []):
                w = edge.get("weight", 0.0)
                desc = f" — {edge['description']}" if edge.get("description") else ""
                lines.append(f"  ->[{edge['relation']}]-> {edge['target_name']} (w={w:.2f}){desc}")
            for edge in e.get("edges_in", []):
                w = edge.get("weight", 0.0)
                desc = f" — {edge['description']}" if edge.get("description") else ""
                lines.append(f"  <-[{edge['relation']}]<- {edge['source_name']} (w={w:.2f}){desc}")
            return "\n".join(lines)

        def _render_neighbor(e: dict, score: float) -> str:
            return (
                f"[{_get(e, 'entity_type')}] {_get(e, 'name')} "
                f"(linked, w={score:.2f}) — {_get(e, 'description')}"
            )

        def _tok(text: str) -> int:
            return len(text) // 4

        sections: list[str] = []
        used = _tok("<memory>\n\n</memory>")
        for e in pinned:
            block = _render_pinned(e)
            used += _tok(block) + 1
            sections.append(block)

        neighbor_scores: dict[str, float] = {}
        for e in pinned:
            for edge in e.get("edges_out", []):
                tgt = edge["target_uuid"]
                if tgt not in pinned_uuids:
                    w = float(edge.get("weight", 0.0))
                    neighbor_scores[tgt] = max(neighbor_scores.get(tgt, 0.0), w)
            for edge in e.get("edges_in", []):
                src = edge["source_uuid"]
                if src not in pinned_uuids:
                    w = float(edge.get("weight", 0.0))
                    neighbor_scores[src] = max(neighbor_scores.get(src, 0.0), w)

        for uid, score in sorted(neighbor_scores.items(), key=lambda x: x[1], reverse=True):
            entity = gdb.get_entity_slim(uid)
            if not entity:
                continue
            block = _render_neighbor(entity, score)
            cost = _tok(block) + 1
            if used + cost > budget:
                break
            used += cost
            sections.append(block)

        body = "\n\n".join(sections)
        return f"<memory>\n{body}\n</memory>"

    cycle.context.register_prompt(
        "memory_pinned",
        lambda _ctx: _build_memory_block(_graph_db, _token_budget),
        role="system",
        priority=_pinned_prio,
    )

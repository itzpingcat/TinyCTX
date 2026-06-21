"""
modules/memory/__main__.py

Registers the memory module.

register_runtime(runtime) — called once at startup:
  1. Resolves config, builds embedder, LLM, ConversationDB
  2. Creates GraphDatabase (owns the ladybug.Database lifetime)
  3. Builds LibrarianRunner (gets write conn from GraphDatabase) and starts
     the poll loop
  4. Builds GraphDB (gets read conn from GraphDatabase) for agent tools
  5. Inits tools module globals

register_agent(cycle) — called per AgentCycle after tool_handler is ready:
  1. Registers kg_* read tools + call_librarian on cycle.tool_handler
  2. Registers pinned entity PromptProvider on cycle.context
  3. Registers pressure-ingest post_turn_hook on cycle

GraphDatabase in graph.py is the single owner of the ladybug.Database and
the only place that opens, checkpoints, or closes it.  LibrarianRunner is
the sole writer; GraphDB is the sync reader.

Shutdown order (enforced by _shutdown()):
  1. Cancel the librarian background task (stop accepting new writes)
  2. Close the GraphDB read connection
  3. GraphDatabase.close() — checkpoints then closes the DB

Pressure-based ingest
---------------------
After each turn, tokens written to the DB on this branch (assistant +
tool nodes) are accumulated in session state under the key
'memory_tokens_since_ingest'. When the total crosses
ingest_pressure_ratio * config.context (floored by ingest_pressure_min_tokens),
a "branch" queue message is dispatched so the librarian ingests only this
branch's unvisited nodes, and the counter resets to zero.

WAL checkpointing
-----------------
Ladybug's default CHECKPOINT_THRESHOLD is 16 MB. A small knowledge graph
never reaches this, so without intervention the WAL grows indefinitely and
the main .lbug files are only written at shutdown.

Two mitigations:
  1. graph.py lowers the threshold to 1 MB at schema-init time so automatic
     checkpoints fire after modest write batches.
  2. Every agent task created in _poll_cycle has a done-callback that calls
     GraphDatabase.checkpoint(), flushing the WAL as soon as the write lock
     is released and the task has committed its transaction.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

# Module-level singletons set by register_runtime.
_graph_database: "GraphDatabase | None" = None   # owns the ladybug.Database
_runner:         "LibrarianRunner | None" = None
_workspace:      Path | None = None
_graph_db:       object | None = None
_tools:          "ModuleType | None" = None
_pinned_prio:    int = 5
_token_budget:   int = 4096
_pinned_user_scan: int = 3
_graph_embedder: object | None = None  # embedder for dedup; falls back to search embedder

# Cache for the pinned-entity memory block. Built in a thread via
# _refresh_memory_block() so the prompt provider never blocks the event loop.
_memory_block_cache: dict[str, str | None] = {"value": None}


# ---------------------------------------------------------------------------
# LibrarianRunner — in-process background task
# ---------------------------------------------------------------------------

class LibrarianRunner:
    """
    Runs the librarian poll loop as an asyncio background task.

    Receives a GraphDatabase from which it creates its AsyncConnection.
    On a mid-session WAL error it asks GraphDatabase to rebuild and then
    refreshes its own write connection.
    """

    def __init__(
        self,
        cfg: dict,
        graph_database: "GraphDatabase",
        log_path: Path,
        conv_db,
        llm,
        embedder,
        graph_embedder=None,
        runtime=None,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)

        self._cfg            = cfg
        self._graph_database = graph_database

        self._write_conn = graph_database.new_async_write_conn()
        self._write_lock = asyncio.Lock()

        self._conv_db       = conv_db
        self._embedder      = embedder
        self._runtime       = runtime  # used to yield to user-facing cycles
        # graph_embedder is used exclusively for dedup similarity.
        # Falls back to the regular search embedder when None.
        self._graph_embedder = graph_embedder if graph_embedder is not None else embedder

        # Background calls go through the same ai.py priority queue as the
        # user-facing cycle (see librarian_agents.py / dedup_agents.py call
        # sites, priority=15) — that's what makes background work wait its
        # turn now, so no wrapper is needed here.
        self._llm = llm

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
            "last_decay_ts": 0.0,
        }
        self._active_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # WAL rebuild — delegates to GraphDatabase, then refreshes write conn
    # ------------------------------------------------------------------

    def _rebuild_write_conn(self) -> None:
        """
        Called when a WAL error is detected on the write path.
        Asks GraphDatabase to rebuild, then opens a fresh write connection.
        """
        self._graph_database.rebuild(stale_write_conn=self._write_conn)
        self._write_conn = self._graph_database.new_async_write_conn()
        logger.info("[memory/librarian] write connection refreshed after WAL rebuild")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the poll loop as a background asyncio task."""
        self._task = asyncio.create_task(self._run(), name="knowledge-librarian")
        logger.info("[memory] LibrarianRunner started")

    def stop(self) -> None:
        """Cancel the background task. Checkpoint is handled by _shutdown()."""
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

    def _checkpoint_callback(self, task: asyncio.Task) -> None:
        """
        Done-callback attached to every agent task.
        Flushes the WAL to the main .lbug files once the task has committed
        its writes and released the write lock.
        """
        self._graph_database.checkpoint()

    def _user_cycles_active(self) -> bool:
        """True when at least one user-facing AgentCycle is running."""
        return self._runtime is not None and self._runtime._active > 0

    async def _poll_cycle(self) -> None:
        from TinyCTX.modules.memory.librarian_agents import (
            run_buffer_agent, run_targeted_agent, run_dedup_cycle,
            nodes_to_text,
        )
        from TinyCTX.modules.memory.dedup_agents import run_edge_dedup
        from TinyCTX.modules.memory.decay import run_decay_sweep

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
                    t.add_done_callback(self._checkpoint_callback)
                    self._active_tasks.add(t)
                elif prompt:
                    logger.warning("[memory/librarian] concurrency cap reached, dropping targeted msg")

            elif msg_type == "branch":
                # Pressure-triggered ingest for a single branch tail.
                # Flag and ingest only the unvisited nodes on this branch.
                tail_id = msg.get("tail_node_id", "").strip()
                if tail_id and len(self._active_tasks) < max_concurrent:
                    async with self._write_lock:
                        flagged_ids = self._conv_db.flag_branch(tail_id, "librarian_visited")
                    if flagged_ids:
                        batch_text, agent_name = nodes_to_text(
                            self._conv_db, list(reversed(flagged_ids)), batch_size
                        )
                        if batch_text.strip():
                            t = asyncio.create_task(
                                run_buffer_agent(
                                    self._cfg, self._write_conn, self._write_lock,
                                    self._llm, batch_text, agent_name, self.agent_logger,
                                )
                            )
                            t.add_done_callback(self._checkpoint_callback)
                            self._active_tasks.add(t)
                            logger.info(
                                "[memory/librarian] pressure ingest: dispatched agent for %d node(s) on branch %s",
                                len(flagged_ids), tail_id[:8],
                            )
                    else:
                        logger.debug(
                            "[memory/librarian] pressure ingest: no unvisited nodes on branch %s", tail_id[:8]
                        )
                elif tail_id:
                    logger.warning("[memory/librarian] concurrency cap reached, dropping branch msg for %s", tail_id[:8])

        # Node walk on schedule — skip when user cycles are active
        now = time.time()
        interval_secs = float(self._cfg.get("trigger_interval_hours", 6)) * 3600
        if (
            not self._user_cycles_active()
            and (now - self._state["last_poll_ts"]) >= interval_secs
        ):
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
                t.add_done_callback(self._checkpoint_callback)
                self._active_tasks.add(t)
                logger.info("[memory/librarian] dispatched agent for %d node(s)", len(flagged_ids))

        # Dedup on schedule — skip when user cycles are active
        dedup_enabled  = bool(self._cfg.get("dedup_enabled", True))
        dedup_interval = float(self._cfg.get("dedup_interval_hours", 24)) * 3600
        if (
            dedup_enabled
            and not self._user_cycles_active()
            and not self._state["dedup_running"]
            and (now - self._state["last_dedup_ts"]) >= dedup_interval
            and len(self._active_tasks) < max_concurrent
        ):
            self._state["dedup_running"] = True
            self._state["last_dedup_ts"] = now

            # Edge dedup runs unconditionally — no embedder required.
            t = asyncio.create_task(
                run_edge_dedup(
                    self._write_conn, self._write_lock, self.agent_logger,
                )
            )
            t.add_done_callback(self._checkpoint_callback)
            self._active_tasks.add(t)

            # Entity dedup requires an embedder.
            if (
                self._embedder is not None
                and len(self._active_tasks) < max_concurrent
            ):
                t = asyncio.create_task(
                    run_dedup_cycle(
                        self._cfg, _workspace, self._write_conn, self._write_lock,
                        self._llm, self._graph_embedder, self.agent_logger,
                    )
                )
                t.add_done_callback(lambda _: self._state.__setitem__("dedup_running", False))
                t.add_done_callback(self._checkpoint_callback)
                self._active_tasks.add(t)
            else:
                self._state["dedup_running"] = False

        # Decay sweep on schedule — skip when user cycles are active.
        # Hard-deletes non-pinned entities scoring below decay_threshold based
        # on priority, distance to nearest pinned entity, edge count, mention
        # count, and read/update recency. Runs fully automatically.
        decay_enabled  = bool(self._cfg.get("decay_enabled", True))
        decay_interval = float(self._cfg.get("decay_interval_hours", 24)) * 3600
        if (
            decay_enabled
            and not self._user_cycles_active()
            and (now - self._state["last_decay_ts"]) >= decay_interval
            and len(self._active_tasks) < max_concurrent
        ):
            self._state["last_decay_ts"] = now

            t = asyncio.create_task(
                run_decay_sweep(
                    self._cfg, self._write_conn, self._write_lock, self.agent_logger,
                )
            )
            t.add_done_callback(self._checkpoint_callback)
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
# _count_entry_tokens — token count for a single HistoryEntry
# ---------------------------------------------------------------------------

def _count_entry_tokens(entry) -> int:
    """
    Estimate tokens for a HistoryEntry using the same char-div-4 heuristic
    used elsewhere in the codebase. Applied to content + serialised tool_calls.
    """
    content = entry.content
    if isinstance(content, list):
        text = json.dumps(content, ensure_ascii=False)
    else:
        text = str(content or "")

    total = len(text) // 4

    if entry.tool_calls:
        total += len(json.dumps(entry.tool_calls, ensure_ascii=False)) // 4

    return total


# ---------------------------------------------------------------------------
# register_runtime — one-time startup
# ---------------------------------------------------------------------------

def register_runtime(runtime) -> None:
    global _graph_database, _runner, _workspace, _graph_db, _tools, _pinned_prio, _token_budget, _pinned_user_scan, _graph_embedder

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

    ws = _workspace
    assert ws is not None

    def _resolve(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ws / p

    graph_path     = _resolve(cfg["graph_path"])
    log_path       = _resolve(cfg.get("librarian_log", "memory/librarian.log"))
    agent_db       = ws / "agent.db"
    max_concurrent = int(cfg.get("max_concurrent", 4))

    _pinned_prio  = int(cfg.get("pinned_priority", 5))
    _token_budget = int(cfg.get("memory_block_tokens", 4096))
    _pinned_user_scan = int(cfg.get("pinned_user_scan", 3))

    # GraphDatabase — single owner of the ladybug.Database
    from TinyCTX.modules.memory.graph import GraphDatabase, GraphDB
    _graph_database = GraphDatabase(graph_path, max_concurrent=max_concurrent)

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

    # Graph embedder (dedup) — falls back to search embedder when not configured
    graph_embedder        = None
    graph_embedding_model = cfg.get("graph_embedding_model", "").strip()
    if graph_embedding_model and graph_embedding_model != embedding_model:
        try:
            from TinyCTX.ai import Embedder
            gemb_cfg       = runtime.config.get_embedding_model(graph_embedding_model)
            graph_embedder = Embedder.from_config(gemb_cfg)
            logger.info("[memory] graph embedder: %s @ %s", gemb_cfg.model, gemb_cfg.base_url)
        except (KeyError, ValueError) as exc:
            logger.warning(
                "[memory] graph_embedding_model '%s' not usable (%s)"
                " â falling back to search embedder for dedup",
                graph_embedding_model, exc,
            )
    # None here means LibrarianRunner falls back to the search embedder

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

    # LibrarianRunner — gets write conn from GraphDatabase
    _runner = LibrarianRunner(cfg, _graph_database, log_path, conv_db, llm, embedder, graph_embedder, runtime=runtime)

    # GraphDB — gets read conn from GraphDatabase
    _graph_db = GraphDB(_graph_database)

    # tools
    import TinyCTX.modules.memory.tools as tools_mod
    _tools = tools_mod
    _tools.init(
        _runner._write_conn, _runner._write_lock, _graph_db, embedder,
        query_template=cfg.get("embed_query_template", "{text}"),
        doc_template=cfg.get("embed_document_template", "{text}"),
        bm25_weight=float(cfg.get("bm25_weight", 0.4)),
    )

    # Shutdown: stop writer → close read conn → checkpoint + close DB.
    # Registered on atexit and both termination signals so clean exits always
    # flush the WAL regardless of how the process is stopped.
    _shutdown_called = [False]

    def _shutdown() -> None:
        if _shutdown_called[0]:
            return
        _shutdown_called[0] = True
        if _runner is not None:
            _runner.stop()           # cancel background task
        if _graph_db is not None:
            _graph_db.close()        # close read connection
        if _graph_database is not None:
            _graph_database.close()  # checkpoint then close DB

    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda *_: _shutdown())
    signal.signal(signal.SIGINT,  lambda *_: _shutdown())

    _runner.start()

    # Register /memory librarian slash command
    async def _cmd_librarian(args: list, context: dict) -> None:
        """
        /memory librarian [prompt]

        With no args: trigger an immediate node-ingest pass.
        With args: queue a targeted agent with the joined args as the prompt.
          e.g. /memory librarian remember that Alice prefers dark mode
        """
        prompt = " ".join(args).strip()
        result = await call_librarian(prompt=prompt)
        send = context.get("send")
        if callable(send):
            await send(result)

    runtime.commands.register(
        "memory", "librarian", _cmd_librarian,
        help="Trigger the memory librarian. Optional: pass a prompt for a targeted update.",
    )

    logger.info(
        "[memory] ready — graph: %s | embedder: %s",
        graph_path, embedding_model or "none",
    )


# ---------------------------------------------------------------------------
# _active_users_from_dialogue — collect recent participants
# ---------------------------------------------------------------------------

def _active_users_from_dialogue(dialogue: list, scan: int) -> set[str]:
    """
    Walk dialogue backwards, collecting author_id from user-role entries.
    Stop after finding `scan` distinct user messages.
    Returns a set of TinyCTX usernames seen in those recent turns.
    """
    from TinyCTX.context import ROLE_USER
    active: set[str] = set()
    count = 0
    for entry in reversed(dialogue):
        if entry.role == ROLE_USER and entry.author_id:
            active.add(entry.author_id)
            count += 1
            if count >= scan:
                break
    return active


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

    cycle.tool_handler.register_tool(call_librarian, always_on=True, min_permission=35)

    # ------------------------------------------------------------------
    # Pressure-based ingest hook
    # ------------------------------------------------------------------

    # Config values captured at registration time.
    try:
        from TinyCTX.modules.memory import EXTENSION_META
        _cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        _cfg = {}

    overrides: dict = {}
    if hasattr(cycle, "config") and hasattr(cycle.config, "extra") and isinstance(cycle.config.extra, dict):
        overrides = cycle.config.extra.get("memory", {})
    _cfg = {**_cfg, **overrides}

    pressure_ratio     = float(_cfg.get("ingest_pressure_ratio", 0.5))
    pressure_min       = int(_cfg.get("ingest_pressure_min_tokens", 500))
    token_limit        = cycle.context.token_limit
    trigger_threshold  = int(pressure_ratio * token_limit)

    # Snapshot of dialogue length before the turn's new nodes are appended.
    # Entries added during the turn (assistant + tool results) sit beyond this index.
    pre_turn_dialogue_len = len(cycle.context.dialogue)

    async def _pressure_hook(final_tail: str) -> None:
        if pressure_ratio <= 0:
            return

        # Count tokens on entries written during this turn only.
        new_entries = cycle.context.dialogue[pre_turn_dialogue_len:]
        turn_tokens = sum(_count_entry_tokens(e) for e in new_entries)

        if turn_tokens == 0:
            return

        # Load accumulated counter from session state (already parsed by cycle start).
        session      = cycle.context.state.get("session", {})
        tokens_since = int(session.get("memory_tokens_since_ingest", 0))
        tokens_since += turn_tokens

        if tokens_since >= max(trigger_threshold, pressure_min):
            tokens_since = 0
            assert _runner is not None
            _runner.queue.put_nowait({"type": "branch", "tail_node_id": final_tail})
            logger.info(
                "[memory] pressure ingest queued for branch %s (threshold=%d)",
                final_tail[:8], trigger_threshold,
            )

        # Persist updated counter as a state_delta on the final tail node.
        cycle.db.update_node_state_delta(
            final_tail,
            json.dumps({"memory_tokens_since_ingest": tokens_since}),
        )

    cycle.post_turn_hooks.append(_pressure_hook)

    # ------------------------------------------------------------------
    # Pinned entity memory block
    # ------------------------------------------------------------------

    def _build_memory_block(gdb, budget: int, active_users: set[str]) -> str | None:
        pinned = gdb.get_pinned_entities_full()
        if not pinned:
            return None

        def _get(e: dict, field: str) -> str:
            return str(e.get(f"e.{field}", e.get(field, "")) or "")

        # Filter by pinned_target: global always included, user-scoped only
        # when that username appears in the recent active_users set.
        def _is_visible(e: dict) -> bool:
            target = _get(e, "pinned_target")
            if target == "global":
                return True
            return target in active_users

        pinned = [e for e in pinned if _is_visible(e)]
        if not pinned:
            return None

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

    # Async helper: build the memory block in a thread and cache the result.
    async def _refresh_memory_block(dialogue_snapshot: list) -> None:
        loop = asyncio.get_running_loop()
        active_users = _active_users_from_dialogue(dialogue_snapshot, _pinned_user_scan)
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _build_memory_block(_graph_db, _token_budget, active_users),
            )
            _memory_block_cache["value"] = result
        except Exception:
            logger.exception("[memory] _refresh_memory_block failed")

    # Kick off an initial refresh so the cache is warm before the first turn.
    asyncio.get_event_loop().create_task(
        _refresh_memory_block(list(cycle.context.dialogue))
    )

    # Add a post_turn_hook that refreshes the cache after each turn.
    # Runs after pressure ingest hook so it picks up the latest dialogue.
    async def _memory_cache_refresh_hook(final_tail: str) -> None:
        await _refresh_memory_block(list(cycle.context.dialogue))

    cycle.post_turn_hooks.append(_memory_cache_refresh_hook)

    cycle.context.register_prompt(
        "memory_pinned",
        lambda _ctx: _memory_block_cache["value"],
        role="system",
        priority=_pinned_prio,
    )
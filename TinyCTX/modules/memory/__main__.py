"""
modules/memory/__main__.py

Wiring for the v2 memory module.

register_runtime(runtime) — once at startup:
  1. Resolve config; build embedder, LLM, ConversationDB.
  2. Create GraphDatabase (owns ladybug.Database + VectorIndex); warm the index.
  3. Build LibrarianRunner (extractor / reviewer / deduper) and start its loop.
  4. Build GraphDB (sync reads) and init tools globals.

register_agent(cycle) — per AgentCycle:
  1. Register search_memory, memory_stats, call_librarian (scope-bound).
  2. Register the passive-RAG + pinned <memory> PromptProvider.
  3. Register the pressure-ingest post_turn_hook.

GraphDatabase is the single owner of the ladybug.Database; the LibrarianRunner
is the sole writer; GraphDB is the sync reader; every writer shares one
asyncio write lock.
"""
from __future__ import annotations

import asyncio
import atexit
import functools
import inspect
import json
import logging
import signal
import time
from pathlib import Path

from TinyCTX.modules.memory import scopes as _scopes
from TinyCTX.modules.memory.format import format_entity
from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)

# Singletons set by register_runtime.
_graph_database = None
_runner = None
_workspace: Path | None = None
_data_path: Path | None = None
_graph_db = None
_tools = None
_cfg: dict = {}



def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base. Nested dicts merge
    key-by-key (so e.g. overriding config.reviewer.enabled doesn't drop the
    rest of the reviewer defaults); any other value type is replaced outright."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# LibrarianRunner
# ---------------------------------------------------------------------------

class LibrarianRunner:
    def __init__(self, cfg, graph_database, log_path, conv_db, llm, embedder,
                 runtime=None, data_path: Path | None = None):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._cfg = cfg
        self._graph_database = graph_database
        self._data_path = data_path
        self._write_conn = graph_database.new_async_write_conn()
        self._write_lock = asyncio.Lock()
        self._conv_db = conv_db
        self._embedder = embedder
        self._runtime = runtime
        self._llm = llm

        self.agent_logger = logging.getLogger("memory.librarian.agent")
        if not self.agent_logger.handlers:
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            self.agent_logger.addHandler(fh)
            self.agent_logger.setLevel(logging.DEBUG)
            self.agent_logger.propagate = False

        self.queue: asyncio.Queue = asyncio.Queue()
        self._task = None
        self._state = {"last_poll_ts": 0.0, "last_dedup_ts": 0.0, "last_review_ts": 0.0,
                       "dedup_running": False}
        self._active_tasks: set = set()
        self._review_queue = None  # lazy (needs data_path)

        # Sentinel file recording last-run timestamps for each background
        # process, so a restart doesn't cause every process to fire at once.
        # Lives in the data dir (not the workspace) — it's runtime bookkeeping,
        # not conversation/graph content.
        self._sentinel_path = Path(self._data_path) / "librarian_state.json" if self._data_path else None
        self._load_sentinel()

    # -- sentinel (last-run timestamps, survives restarts) --
    def _load_sentinel(self):
        if self._sentinel_path is None or not self._sentinel_path.exists():
            return
        try:
            saved = json.loads(self._sentinel_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[memory/librarian] sentinel unreadable, ignoring: %s", self._sentinel_path)
            return
        for key in ("last_poll_ts", "last_dedup_ts", "last_review_ts"):
            val = saved.get(key)
            if isinstance(val, (int, float)):
                self._state[key] = float(val)
        logger.info("[memory/librarian] loaded sentinel: %s", self._sentinel_path)

    def _save_sentinel(self):
        if self._sentinel_path is None:
            return
        try:
            self._sentinel_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._sentinel_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "last_poll_ts": self._state["last_poll_ts"],
                "last_dedup_ts": self._state["last_dedup_ts"],
                "last_review_ts": self._state["last_review_ts"],
            }), encoding="utf-8")
            tmp.replace(self._sentinel_path)
        except Exception as exc:
            logger.warning("[memory/librarian] cannot write sentinel file: %s", exc)

    # -- lifecycle --
    def start(self):
        self._task = asyncio.create_task(self._run(), name="knowledge-librarian")
        logger.info("[memory] LibrarianRunner started")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self):
        try:
            while True:
                try:
                    await self._poll_cycle()
                except Exception:
                    logger.exception("[memory/librarian] poll cycle error")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            if self._active_tasks:
                await asyncio.gather(*self._active_tasks, return_exceptions=True)
            logger.info("[memory/librarian] stopped")

    def _checkpoint_cb(self, _task):
        self._graph_database.checkpoint()

    def _user_cycles_active(self) -> bool:
        return self._runtime is not None and getattr(self._runtime, "_active", 0) > 0

    def _review_q(self):
        from TinyCTX.modules.memory.reviewer import ReviewerQueue
        if self._review_queue is None:
            self._review_queue = ReviewerQueue(Path(self._data_path) / "reviewer_queue.json")
        return self._review_queue

    async def _poll_cycle(self):
        from TinyCTX.modules.memory.extractor import run_extractor, resolve_extractor_scopes
        from TinyCTX.modules.memory.reviewer import run_reviewer_cycle
        from TinyCTX.modules.memory.deduper import run_dedup_cycle
        from TinyCTX.modules.memory.librarian_common import nodes_to_text

        done = {t for t in self._active_tasks if t.done()}
        for t in done:
            if not t.cancelled() and t.exception():
                logger.error("[memory/librarian] task raised: %s", t.exception())
        self._active_tasks -= done

        librarian_cfg = self._cfg.get("librarian", {})
        max_concurrent = int(librarian_cfg.get("max_concurrent", 4))
        batch_size = int(librarian_cfg.get("batch_size", 20))
        overlap_nodes = int(librarian_cfg.get("overlap_nodes", 5))

        # -- queue messages (targeted / branch / trigger / review) --
        while not self.queue.empty():
            msg = self.queue.get_nowait()
            mtype = msg.get("type")
            if mtype == "branch":
                await self._dispatch_branch(msg.get("tail_node_id", "").strip(),
                                            run_extractor, resolve_extractor_scopes,
                                            nodes_to_text, batch_size, max_concurrent,
                                            overlap_nodes)
            elif mtype == "review_front":
                issue = msg.get("issue")
                if issue:
                    await self._review_q().push_front(issue)

        now = time.time()

        # -- scheduled node walk (extractor) --
        interval = float(librarian_cfg.get("trigger_interval_hours", 6)) * 3600
        if not self._user_cycles_active() and (now - self._state["last_poll_ts"]) >= interval:
            self._state["last_poll_ts"] = now
            self._save_sentinel()
            async with self._write_lock:
                tails = self._conv_db.get_tail_nodes()
            for tail in tails:
                if len(self._active_tasks) >= max_concurrent:
                    break
                await self._dispatch_branch(tail.id, run_extractor, resolve_extractor_scopes,
                                            nodes_to_text, batch_size, max_concurrent,
                                            overlap_nodes)

        # -- reviewer cycle --
        reviewer_cfg = self._cfg.get("reviewer", {})
        if (bool(reviewer_cfg.get("enabled", True))
                and not self._user_cycles_active()
                and (now - self._state["last_review_ts"]) >= float(reviewer_cfg.get("interval_hours", 6)) * 3600
                and len(self._active_tasks) < max_concurrent):
            self._state["last_review_ts"] = now
            self._save_sentinel()
            t = asyncio.create_task(run_reviewer_cycle(
                self._cfg, _graph_db, self._write_conn, self._write_lock, self._llm,
                self._review_q(), self.agent_logger))
            t.add_done_callback(self._checkpoint_cb)
            self._active_tasks.add(t)

        # -- deduper cycle --
        dedup_cfg = self._cfg.get("dedup", {})
        if (bool(dedup_cfg.get("enabled", True))
                and self._embedder is not None
                and not self._user_cycles_active()
                and not self._state["dedup_running"]
                and (now - self._state["last_dedup_ts"]) >= float(dedup_cfg.get("interval_hours", 6)) * 3600
                and len(self._active_tasks) < max_concurrent):
            self._state["dedup_running"] = True
            self._state["last_dedup_ts"] = now
            self._save_sentinel()
            t = asyncio.create_task(run_dedup_cycle(
                self._cfg, self._data_path, self._write_conn, self._write_lock, self._llm,
                self._embedder, _graph_db, self.agent_logger))
            t.add_done_callback(lambda _: self._state.__setitem__("dedup_running", False))
            t.add_done_callback(self._checkpoint_cb)
            self._active_tasks.add(t)

    async def _dispatch_branch(self, tail_id, run_extractor, resolve_scopes_fn,
                               nodes_to_text, batch_size, max_concurrent, overlap_nodes=0):
        if not tail_id or len(self._active_tasks) >= max_concurrent:
            return
        async with self._write_lock:
            flagged = self._conv_db.flag_branch(tail_id, "librarian_visited")
        if not flagged:
            return
        ordered = list(reversed(flagged))
        overlap_ids = self._overlap_context(ordered[0], overlap_nodes) if overlap_nodes > 0 else []
        batch_text, agent_name = nodes_to_text(self._conv_db, ordered, batch_size, overlap_ids)
        if not batch_text.strip():
            return
        authors = self._branch_authors(ordered[:batch_size])
        env = self._branch_env(tail_id)
        scope_set = resolve_scopes_fn(env, authors)
        t = asyncio.create_task(run_extractor(
            self._cfg, self._write_conn, self._write_lock, self._llm,
            batch_text, agent_name, scope_set, self.agent_logger))
        t.add_done_callback(self._checkpoint_cb)
        self._active_tasks.add(t)
        logger.info("[memory/librarian] extractor dispatched for %d node(s), scopes=%s",
                    len(flagged), sorted(scope_set))

    def _overlap_context(self, first_new_node_id: str, overlap_nodes: int) -> list[str]:
        """
        Return up to `overlap_nodes` already-visited ancestor node ids immediately
        preceding first_new_node_id, oldest-first. Gives the extractor trailing
        context for small/fragmented new batches without re-extracting them.
        """
        node = self._conv_db.get_node(first_new_node_id)
        if node is None or node.parent_id is None:
            return []
        ancestors = self._conv_db.get_ancestors(node.parent_id)
        return [n.id for n in ancestors[-overlap_nodes:]]

    def _branch_authors(self, node_ids) -> set:
        authors = set()
        for nid in node_ids:
            node = self._conv_db.get_node(nid)
            if node and node.role == "user" and node.author_id:
                authors.add(node.author_id)
        return authors

    def _branch_env(self, tail_id) -> dict:
        try:
            return {"server_name": self._conv_db.get_state(tail_id, "server_name", None)}
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# call_librarian (main agent tool)
# ---------------------------------------------------------------------------

async def call_librarian(prompt: str = "") -> str:
    """
    Ask the background librarian to review or update memory. With a prompt, the
    issue is pushed to the FRONT of the reviewer queue for prompt handling. With
    no prompt, an immediate conversation-ingest pass is triggered.

    Args:
        prompt: Optional instruction describing what to review or fix.
    """
    assert _runner is not None
    if prompt.strip():
        issue = {"flagger_type": "manual", "entity_uuids": [], "scope": "global", "detail": prompt.strip()}
        _runner.queue.put_nowait({"type": "review_front", "issue": issue})
        return f"Librarian: queued for priority review — '{prompt[:60]}'"
    tails = _runner._conv_db.get_tail_nodes()
    if tails:
        _runner.queue.put_nowait({"type": "branch", "tail_node_id": tails[0].id})
    return "Librarian: ingest triggered"


# ---------------------------------------------------------------------------
# register_runtime
# ---------------------------------------------------------------------------

def _count_entry_tokens(entry) -> int:
    content = entry.content
    text = json.dumps(content, ensure_ascii=False) if isinstance(content, list) else str(content or "")
    total = len(text) // 4
    if entry.tool_calls:
        total += len(json.dumps(entry.tool_calls, ensure_ascii=False)) // 4
    return total


def register_runtime(runtime) -> None:
    global _graph_database, _runner, _workspace, _data_path, _graph_db, _tools, _cfg

    _workspace = Path(runtime.config.workspace.path).expanduser().resolve()
    _workspace.mkdir(parents=True, exist_ok=True)

    data_path = getattr(runtime, "data_path", None)
    if data_path is None:
        data_path = Path(runtime.config.data.path).expanduser().resolve()
    data_path.mkdir(parents=True, exist_ok=True)
    _data_path = data_path

    from TinyCTX.modules.memory import EXTENSION_META
    defaults = EXTENSION_META.get("default_config", {})
    overrides = {}
    if hasattr(runtime.config, "extra") and isinstance(runtime.config.extra, dict):
        overrides = runtime.config.extra.get("memory", {})
    cfg = _deep_merge(defaults, overrides)
    _cfg = cfg

    def _resolve(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else data_path / p

    graph_path = _resolve(cfg["graph_path"])
    log_path = _resolve(cfg.get("librarian_log", "memory/librarian.log"))
    agent_db = data_path / "agent.db"
    max_concurrent = int(cfg.get("librarian", {}).get("max_concurrent", 4))

    # One-shot migration from v1 if present.
    try:
        from TinyCTX.modules.memory.migrate import migrate
        old_path = graph_path.parent / "graph.lbug"
        if old_path.exists() and not graph_path.exists():
            summary = migrate(old_path, graph_path)
            logger.info("[memory] migration: %s", summary)
    except Exception as exc:
        logger.warning("[memory] migration skipped/failed: %s", exc)

    from TinyCTX.modules.memory.graph import GraphDatabase, GraphDB
    _graph_database = GraphDatabase(graph_path, max_concurrent=max_concurrent)
    _graph_database.warm_index()

    embedder = None
    emb_model = cfg.get("embedding_model", "").strip()
    if emb_model:
        try:
            from TinyCTX.ai import Embedder
            embedder = Embedder.from_config(runtime.config.get_embedding_model(emb_model))
            logger.info("[memory] embedder: %s", emb_model)
        except (KeyError, ValueError) as exc:
            logger.warning("[memory] embedding_model '%s' unusable (%s)", emb_model, exc)

    primary = runtime.config.llm.primary
    lib_key = cfg.get("librarian", {}).get("model", "").strip() or primary
    mc = runtime.config.models.get(lib_key)
    try:
        api_key = mc.api_key if mc else ""
    except EnvironmentError:
        api_key = ""
    from TinyCTX.ai import LLM
    llm = LLM(base_url=mc.base_url if mc else "", api_key=api_key, model=mc.model if mc else "",
              max_tokens=mc.max_tokens if mc else 2048, temperature=mc.temperature if mc else 0.7)

    from TinyCTX.db import ConversationDB
    conv_db = ConversationDB(agent_db)
    atexit.register(conv_db.close)

    _runner = LibrarianRunner(cfg, _graph_database, log_path, conv_db, llm, embedder,
                              runtime=runtime, data_path=data_path)
    _graph_db = GraphDB(_graph_database)

    import TinyCTX.modules.memory.tools as tools_mod
    _tools = tools_mod
    _tools.init(_runner._write_conn, _runner._write_lock, _graph_db, embedder,
                cfg=cfg, data_dir=data_path)

    _shutdown_called = [False]

    def _shutdown():
        if _shutdown_called[0]:
            return
        _shutdown_called[0] = True
        if _runner is not None:
            _runner.stop()
        if _graph_db is not None:
            _graph_db.close()
        if _graph_database is not None:
            _graph_database.close()

    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda *_: _shutdown())
    signal.signal(signal.SIGINT, lambda *_: _shutdown())
    _runner.start()

    async def _cmd_librarian(args, context):
        result = await call_librarian(prompt=" ".join(args).strip())
        send = context.get("send")
        if callable(send):
            await send(result)

    runtime.commands.register("memory", "librarian", _cmd_librarian,
                              help="Trigger the memory librarian. Optional: a prompt for priority review.",
                              required_permissions={Permission.MEMORY_WRITE})

    async def _cmd_stats(args, context):
        # Diagnostics command: show full totals across every scope in the graph,
        # plus live dedup progress.
        with _tools.scope_context(_graph_db.all_scopes() | {"global"}):
            result = await _tools.memory_stats()
        send = context.get("send")
        if callable(send):
            await send(result)

    runtime.commands.register("memory", "stats", _cmd_stats,
                              help="Show memory graph diagnostics: entity/edge counts, "
                                   "pins, reviewer backlog, and live dedup progress.",
                              required_permissions={Permission.MEMORY_READ})
    logger.info("[memory] ready — graph: %s | embedder: %s", graph_path, emb_model or "none")


# ---------------------------------------------------------------------------
# scope resolution + passive block
# ---------------------------------------------------------------------------

def _active_users(dialogue, scan: int) -> set:
    from TinyCTX.context import ROLE_USER
    active, count = set(), 0
    for entry in reversed(dialogue):
        if entry.role == ROLE_USER and getattr(entry, "author_id", None):
            active.add(entry.author_id)
            count += 1
            if count >= scan:
                break
    return active


def _cycle_env(cycle) -> dict:
    try:
        st = cycle.context.state
        return {"server_name": st.get("server_name")}
    except Exception:
        return {}


def _last_user_text(dialogue) -> str:
    from TinyCTX.context import ROLE_USER
    for entry in reversed(dialogue):
        if entry.role == ROLE_USER:
            c = entry.content
            if isinstance(c, list):
                return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            return str(c or "")
    return ""


def _resolve_cycle_scopes(cycle) -> set:
    scan = int(_cfg.get("pins", {}).get("user_scan", 3))
    return _scopes.resolve_scopes(_cycle_env(cycle), _active_users(cycle.context.dialogue, scan))


async def _build_memory_block(visible: set, last_user_text: str) -> str | None:
    """Assemble the <memory> block: pinned first, then RAG hits, deduped by uuid,
    min-p before RRF, capped at memory_block_tokens."""
    gdb = _graph_db
    passive_cfg = _cfg.get("passive_rag", {})
    budget = int(passive_cfg.get("memory_block_tokens", 2048))
    rag_enabled = bool(passive_cfg.get("enabled", True))
    fmt_cfg = _cfg.get("formatting", {})
    detail = fmt_cfg.get("injection_detail", "low")
    if detail not in ("low", "medium", "high"):
        detail = "low"
    desc_trunc = int(fmt_cfg.get("desc_truncate_chars", 2500))

    def _tok(s: str) -> int:
        return len(s) // 4

    # 1. pinned (most-recent first, already visibility-filtered)
    pinned = gdb.pinned_entities(visible)
    ordered: list[tuple[str, dict]] = [(e["e.uuid"], e) for e in pinned]
    seen = {uid for uid, _ in ordered}

    # 2. RAG hits
    if rag_enabled and last_user_text.strip():
        for uid in await _passive_rag_uuids(visible, last_user_text):
            if uid not in seen:
                ent = gdb.get_entity(uid, visible)
                if ent:
                    ordered.append((uid, ent))
                    seen.add(uid)

    if not ordered:
        return None

    # 3. token cap: pinned first; mark overflow
    lines: list[str] = []
    used = _tok("<memory>\n\n</memory>")
    pinned_dropped = 0
    n_pinned = len(pinned)
    bump = float(passive_cfg.get("mention_bump", 0.1))
    bump_uids: list[str] = []
    for idx, (uid, e) in enumerate(ordered):
        block = format_entity(e, detail=detail, desc_truncate_chars=desc_trunc)
        cost = _tok(block) + 1
        if used + cost > budget:
            if idx < n_pinned:
                pinned_dropped += 1
            continue
        used += cost
        lines.append(block)
        if idx >= n_pinned:   # passive (non-pinned) retrieval bumps mention
            bump_uids.append(uid)
    if bump_uids:
        _tools._bump_mention(bump_uids, bump)
    if pinned_dropped:
        lines.append(f"… {pinned_dropped} pinned entities omitted (token budget)")

    return "<memory>\n" + "\n\n".join(lines) + "\n</memory>" if lines else None


async def _passive_rag_uuids(visible: set, query: str) -> list[str]:
    from TinyCTX.utils.bm25 import BM25
    passive_cfg = _cfg.get("passive_rag", {})
    min_p = float(passive_cfg.get("min_p", 0.30))
    bm25_w = float(passive_cfg.get("bm25_weight", 0.4))
    rrf_k = int(passive_cfg.get("rrf_k", 60))
    top_k = int(_cfg.get("passive_top_k", 5))

    bm25_ranks = {}
    corpus = dict(_graph_db.bm25_corpus(visible))
    if corpus:
        for rank, (uid, score) in enumerate((h for h in BM25(corpus).search(query, top_k=len(corpus)) if h[1] > 0), 1):
            bm25_ranks[uid] = rank

    vec_ranks = {}
    if _runner and _runner._embedder is not None and len(_graph_db.vector_index):
        try:
            qvec = (await _runner._embedder.embed([query], priority=5, kind="query"))[0]
            if qvec is not None:
                allowed = _graph_db.scoped_uuids(visible)
                for rank, (uid, _s) in enumerate(
                        _graph_db.vector_index.search(qvec, k=len(allowed) or top_k, min_p=min_p, allowed=allowed), 1):
                    vec_ranks[uid] = rank
        except Exception as exc:
            logger.warning("[memory] passive vector failed: %s", exc)

    fused = _tools._rrf_fuse(bm25_ranks, vec_ranks, bm25_w=bm25_w, rrf_k=rrf_k)
    return [u for u, _ in fused[:top_k]]


# ---------------------------------------------------------------------------
# register_agent
# ---------------------------------------------------------------------------

def _scope_bound(fn, cycle):
    """Wrap a memory tool so it runs inside the cycle's visible scope, while
    preserving the tool's name/docstring/signature for schema extraction."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with _tools.scope_context(_resolve_cycle_scopes(cycle)):
            return await fn(*args, **kwargs)
    wrapper.__signature__ = inspect.signature(fn)
    return wrapper


def register_agent(cycle) -> None:
    if _runner is None:
        logger.error("[memory] register_agent before register_runtime — skipping")
        return
    assert _tools is not None

    cycle.tool_handler.register_tool(_scope_bound(_tools.search_memory, cycle),
                                     always_on=True, required_permissions={Permission.MEMORY_READ})
    cycle.tool_handler.register_tool(_scope_bound(_tools.memory_stats, cycle),
                                     always_on=False, required_permissions={Permission.MEMORY_READ})
    cycle.tool_handler.register_tool(call_librarian, always_on=True, required_permissions={Permission.MEMORY_WRITE})

    # pressure ingest
    librarian_cfg = _cfg.get("librarian", {})
    pressure_ratio = float(librarian_cfg.get("ingest_pressure_ratio", 0.5))
    pressure_min = int(librarian_cfg.get("ingest_pressure_min_tokens", 500))
    trigger_threshold = int(pressure_ratio * cycle.context.token_limit)
    pre_len = len(cycle.context.dialogue)

    async def _pressure_hook(final_tail: str):
        if pressure_ratio <= 0:
            return
        new_entries = cycle.context.dialogue[pre_len:]
        turn_tokens = sum(_count_entry_tokens(e) for e in new_entries)
        if turn_tokens == 0:
            return
        session = cycle.context.state.get("session", {})
        tokens_since = int(session.get("memory_tokens_since_ingest", 0)) + turn_tokens
        if tokens_since >= max(trigger_threshold, pressure_min):
            tokens_since = 0
            _runner.queue.put_nowait({"type": "branch", "tail_node_id": final_tail})
        cycle.db.set_state(final_tail, "memory_tokens_since_ingest", tokens_since)

    cycle.post_turn_hooks.append(_pressure_hook)

    # passive memory block
    cycle._memory_block = None

    async def _refresh_block(dialogue_snapshot):
        try:
            visible = _scopes.resolve_scopes(_cycle_env(cycle),
                                             _active_users(dialogue_snapshot, int(_cfg.get("pins", {}).get("user_scan", 3))))
            cycle._memory_block = await _build_memory_block(visible, _last_user_text(dialogue_snapshot))
        except Exception:
            logger.exception("[memory] refresh block failed")

    asyncio.get_event_loop().create_task(_refresh_block(list(cycle.context.dialogue)))

    async def _block_refresh_hook(final_tail: str):
        await _refresh_block(list(cycle.context.dialogue))

    cycle.post_turn_hooks.append(_block_refresh_hook)
    cycle.context.register_prompt("memory_block", lambda _ctx: cycle._memory_block,
                                  role="system", priority=int(_cfg.get("pins", {}).get("priority", 5)))

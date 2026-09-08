"""
modules/memory

Wiring for the v2 memory module.

@hook(HookType.STARTUP) — once at process start:
  1. Resolve config; build embedder, LLM, ConversationDB.
  2. Create GraphDatabase (owns ladybug.Database + VectorIndex); warm the index.
  3. Build LibrarianRunner (extractor / reviewer / deduper) and start its loop.
  4. Build GraphDB (sync reads) and init tools globals.
  5. Register the /memory librarian and /memory stats @command handlers.

@hook(HookType.TURN_START) — per AgentCycle (needs live cycle.context/.db):
  1. Register search_memory / memory_stats, scope-bound to this cycle.
  2. Register the pressure-ingest post_turn hook.

call_librarian is a plain @tool — it only touches process-lifetime state
(self._runner), no live cycle needed.

The passive-RAG + pinned <memory> block is a @hook(HookType.PRE_ASSEMBLE_ASYNC)
paired with a @prompt reading ctx.state — see refresh_memory_block's docstring
for the join-point fix to the pre-Module version's race (a detached
background task with no ordering guarantee against assemble()).

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

from TinyCTX.decorators import command, hook, prompt, tool
from TinyCTX.hooks import HookType
from TinyCTX.module import Module
from TinyCTX.modules.memory import scopes as _scopes
from TinyCTX.modules.memory.format import format_entity
from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)


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


def _count_entry_tokens(entry) -> int:
    content = entry.content
    text = json.dumps(content, ensure_ascii=False) if isinstance(content, list) else str(content or "")
    total = len(text) // 4
    if entry.tool_calls:
        total += len(json.dumps(entry.tool_calls, ensure_ascii=False)) // 4
    return total


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


def _ctx_env(ctx) -> dict:
    try:
        st = ctx.state
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


# ---------------------------------------------------------------------------
# LibrarianRunner (unchanged — process-lifetime object owned by the Module)
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
                self._cfg, self._graph_db_for_librarian, self._write_conn, self._write_lock, self._llm,
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
                self._embedder, self._graph_db_for_librarian, self.agent_logger))
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


class Memory(Module):
    """Long-term memory backed by a scoped LadybugDB property graph."""

    settings = {
        "graph_path":    {"default": "memory/memory.lbug", "type": "str", "description": "Relative to the internal data dir."},
        "librarian_log": {"default": "memory/librarian.log", "type": "str", "description": "Relative to the internal data dir."},
        "embedding_model": {"default": "", "type": "str", "description": "Single model; empty for BM25-only."},
        "mention_half_life_days": {"default": 30, "type": "int", "description": "Read-time weighting shared across flaggers."},
        "formatting": {
            "default": {"injection_detail": "low", "desc_truncate_chars": 2500},
            "type": "dict",
            "description": "injection_detail (low/medium/high) and desc_truncate_chars (0 = no truncation) for the <memory> block.",
        },
        "passive_rag": {
            "default": {
                "enabled": True, "memory_block_tokens": 2048, "min_p": 0.30, "search_min_p": 0.0,
                "bm25_weight": 0.40, "rrf_k": 60, "mention_bump": 0.1,
                # New in the Module migration — see refresh_memory_block's
                # docstring for the join-point fix this bounds.
                "block_timeout_seconds": 3.0,
            },
            "type": "dict",
            "description": "Passive RAG (BM25 + vector, min-p before RRF) feeding the <memory> block.",
        },
        "pins": {
            "default": {"include_neighbors": False, "priority": 5, "user_scan": 3, "max_per_scope": 12},
            "type": "dict",
            "description": "Pinned-entity behavior for the <memory> block.",
        },
        "librarian": {
            "default": {
                "trigger_interval_hours": 6, "batch_size": 20, "overlap_nodes": 5, "max_concurrent": 4,
                "model": "", "ingest_pressure_ratio": 0.5, "ingest_pressure_min_tokens": 500, "max_cycles": 40,
            },
            "type": "dict",
            "description": "Background librarian runner (extractor/reviewer/deduper) tuning.",
        },
        "reviewer": {
            "default": {"enabled": True, "interval_hours": 6, "base_delay": 30, "min_delay": 2, "target_len": 10},
            "type": "dict",
            "description": "Reviewer librarian cadence.",
        },
        "flaggers": {
            "default": {
                "max_edges_between": 4, "edge_bloat_min_edges": 10, "edge_bloat_chars_per_edge": 10,
                "desc_max_chars": 1200, "desc_min_chars": 15, "fuzzy_name_threshold": 95,
                "false_alias_max_cosine": 0.5, "decay_min_effective_mention": 0.5,
                "decay_max_edges": 1, "decay_stale_days": 90,
            },
            "type": "dict",
            "description": "Reviewer flagger thresholds.",
        },
        "dedup": {
            "default": {"enabled": True, "interval_hours": 6, "similarity_threshold": 0.85, "batch_count": 8},
            "type": "dict",
            "description": "Deduper cadence and similarity threshold.",
        },
    }

    def resolve_settings(self, extra):
        # Overridden (rather than the base's whole-value replace) for the
        # same reason as modules/system_prompt: every setting here except
        # the four scalars is itself a nested config section, and a caller
        # overriding one subkey (e.g. reviewer.enabled) must not silently
        # drop the rest of that section's defaults.
        resolved = {key: spec.get("default") for key, spec in self.settings.items()}
        overrides = extra.get(self.name, {}) if extra and isinstance(extra, dict) else {}
        for key in resolved:
            if key in overrides:
                if isinstance(resolved[key], dict) and isinstance(overrides[key], dict):
                    resolved[key] = _deep_merge(resolved[key], overrides[key])
                else:
                    resolved[key] = overrides[key]
        return resolved

    # ------------------------------------------------------------------
    # STARTUP
    # ------------------------------------------------------------------

    @hook(HookType.STARTUP)
    def load(self, runtime) -> None:
        cfg = self.config
        self._cfg = cfg

        workspace = Path(runtime.config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self._workspace = workspace

        data_path = getattr(runtime, "data_path", None)
        if data_path is None:
            data_path = Path(runtime.config.data.path).expanduser().resolve()
        data_path.mkdir(parents=True, exist_ok=True)
        self._data_path = data_path

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
        self._graph_database = GraphDatabase(graph_path, max_concurrent=max_concurrent)
        self._graph_database.warm_index()

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

        self._runner = LibrarianRunner(cfg, self._graph_database, log_path, conv_db, llm, embedder,
                                       runtime=runtime, data_path=data_path)
        self._graph_db = GraphDB(self._graph_database)
        # GraphDB wraps the same GraphDatabase and only exists once LibrarianRunner
        # has already been constructed (same order the pre-Module version read it
        # in, via a module global set after register_runtime's LibrarianRunner(...)
        # call) — handed to the runner after the fact rather than reordering
        # construction.
        self._runner._graph_db_for_librarian = self._graph_db

        import TinyCTX.modules.memory.tools as tools_mod
        self._tools = tools_mod
        self._tools.init(self._runner._write_conn, self._runner._write_lock, self._graph_db, embedder,
                         cfg=cfg, data_dir=data_path)

        _shutdown_called = [False]

        def _shutdown():
            if _shutdown_called[0]:
                return
            _shutdown_called[0] = True
            if self._runner is not None:
                self._runner.stop()
            if self._graph_db is not None:
                self._graph_db.close()
            if self._graph_database is not None:
                self._graph_database.close()

        atexit.register(_shutdown)
        signal.signal(signal.SIGTERM, lambda *_: _shutdown())
        signal.signal(signal.SIGINT, lambda *_: _shutdown())
        self._runner.start()

        logger.info("[memory] ready — graph: %s | embedder: %s", graph_path, emb_model or "none")

    # ------------------------------------------------------------------
    # /memory librarian, /memory stats — only need self.* (process-lifetime)
    # + the per-call context dict, so these are genuine @command handlers
    # ------------------------------------------------------------------

    @command("memory", "librarian", permissions={Permission.MEMORY_WRITE},
             help="Trigger the memory librarian. Optional: a prompt for priority review.")
    async def cmd_librarian(self, args: list[str], context: dict) -> str:
        return await self.call_librarian(prompt=" ".join(args).strip())

    @command("memory", "stats", permissions={Permission.MEMORY_READ},
             help="Show memory graph diagnostics: entity/edge counts, pins, reviewer backlog, and live dedup progress.")
    async def cmd_stats(self, args: list[str], context: dict) -> str:
        with self._tools.scope_context(self._graph_db.all_scopes() | {"global"}):
            return await self._tools.memory_stats()

    # ------------------------------------------------------------------
    # call_librarian — process-lifetime state only, plain @tool
    # ------------------------------------------------------------------

    @tool(always_on=True, permissions={Permission.MEMORY_WRITE})
    async def call_librarian(self, prompt: str = "") -> str:
        """
        Ask the background librarian to review or update memory. With a prompt, the
        issue is pushed to the FRONT of the reviewer queue for prompt handling. With
        no prompt, an immediate conversation-ingest pass is triggered.

        Args:
            prompt: Optional instruction describing what to review or fix.
        """
        assert self._runner is not None
        if prompt.strip():
            issue = {"flagger_type": "manual", "entity_uuids": [], "scope": "global", "detail": prompt.strip()}
            self._runner.queue.put_nowait({"type": "review_front", "issue": issue})
            return f"Librarian: queued for priority review — '{prompt[:60]}'"
        tails = self._runner._conv_db.get_tail_nodes()
        if tails:
            self._runner.queue.put_nowait({"type": "branch", "tail_node_id": tails[0].id})
        return "Librarian: ingest triggered"

    # ------------------------------------------------------------------
    # Scope resolution + passive block
    # ------------------------------------------------------------------

    def _resolve_ctx_scopes(self, ctx) -> set:
        scan = int(self.config.get("pins", {}).get("user_scan", 3))
        return _scopes.resolve_scopes(_ctx_env(ctx), _active_users(ctx.dialogue, scan))

    async def _build_memory_block(self, visible: set, last_user_text: str) -> str | None:
        """Assemble the <memory> block: pinned first, then RAG hits, deduped by uuid,
        min-p before RRF, capped at memory_block_tokens."""
        gdb = self._graph_db
        passive_cfg = self.config.get("passive_rag", {})
        budget = int(passive_cfg.get("memory_block_tokens", 2048))
        rag_enabled = bool(passive_cfg.get("enabled", True))
        fmt_cfg = self.config.get("formatting", {})
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
            for uid in await self._passive_rag_uuids(visible, last_user_text):
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
            self._tools._bump_mention(bump_uids, bump)
        if pinned_dropped:
            lines.append(f"… {pinned_dropped} pinned entities omitted (token budget)")

        return "<memory>\n" + "\n\n".join(lines) + "\n</memory>" if lines else None

    async def _passive_rag_uuids(self, visible: set, query: str) -> list[str]:
        from TinyCTX.utils.bm25 import BM25
        passive_cfg = self.config.get("passive_rag", {})
        min_p = float(passive_cfg.get("min_p", 0.30))
        bm25_w = float(passive_cfg.get("bm25_weight", 0.4))
        rrf_k = int(passive_cfg.get("rrf_k", 60))
        top_k = int(self.config.get("passive_top_k", 5))

        bm25_ranks = {}
        corpus = dict(self._graph_db.bm25_corpus(visible))
        if corpus:
            for rank, (uid, score) in enumerate((h for h in BM25(corpus).search(query, top_k=len(corpus)) if h[1] > 0), 1):
                bm25_ranks[uid] = rank

        vec_ranks = {}
        if self._runner and self._runner._embedder is not None and len(self._graph_db.vector_index):
            try:
                qvec = (await self._runner._embedder.embed([query], priority=5, kind="query"))[0]
                if qvec is not None:
                    allowed = self._graph_db.scoped_uuids(visible)
                    for rank, (uid, _s) in enumerate(
                            self._graph_db.vector_index.search(qvec, k=len(allowed) or top_k, min_p=min_p, allowed=allowed), 1):
                        vec_ranks[uid] = rank
            except Exception as exc:
                logger.warning("[memory] passive vector failed: %s", exc)

        fused = self._tools._rrf_fuse(bm25_ranks, vec_ranks, bm25_w=bm25_w, rrf_k=rrf_k)
        return [u for u, _ in fused[:top_k]]

    # ------------------------------------------------------------------
    # <memory> block — PRE_ASSEMBLE_ASYNC computes it, @prompt reads it back
    # ------------------------------------------------------------------
    #
    # Pre-Module version: a detached asyncio.create_task fired at cycle start
    # (register_agent time) computed the block for whatever assemble() call
    # happened to come next, with no ordering guarantee against it — assemble()
    # could run before the task finished, reading cycle._memory_block while it
    # was still None from initialization, or a stale value from a PRIOR
    # assemble() pass within the same multi-step tool-calling cycle. A
    # POST_TURN hook then refreshed it again for whatever the *next* cycle
    # turned out to be, which isn't guaranteed to be the same request under
    # any concurrency.
    #
    # The fix (MODULES-PLAN-P1.md's join-point requirement): PRE_ASSEMBLE_ASYNC
    # is awaited by AgentCycle.run() BEFORE assemble() is called at all, so
    # awaiting the block computation directly here — bounded by a timeout,
    # falling back to whatever assemble() last had for THIS cycle (ctx.state,
    # not dropped between assemble() calls the way Scratch is) rather than
    # None on timeout — makes "the query hadn't returned yet" and "nothing
    # relevant" distinguishable (the former logs a warning) instead of both
    # collapsing into a silently missing block. This also fixes a second bug
    # for free: recomputing on every assemble() pass instead of once per
    # cycle means a later assemble() in the same multi-step tool-calling loop
    # sees a block reflecting the tool calls that already ran, not a stale
    # one from before them.

    @hook(HookType.PRE_ASSEMBLE_ASYNC)
    async def refresh_memory_block(self, ctx) -> None:
        try:
            visible = self._resolve_ctx_scopes(ctx)
            last_user_text = _last_user_text(ctx.dialogue)
            timeout = float(self.config.get("passive_rag", {}).get("block_timeout_seconds", 3.0))
            block = await asyncio.wait_for(
                self._build_memory_block(visible, last_user_text), timeout=timeout,
            )
            ctx.state["memory_block"] = block
        except asyncio.TimeoutError:
            logger.warning(
                "[memory] memory_block refresh timed out after %.1fs — keeping last-known-good "
                "for this cycle (may be None if this is the first assemble() pass)", timeout,
            )
        except Exception:
            logger.exception("[memory] refresh block failed")

    @prompt(role="system", priority=5, name="memory_block")
    def memory_block_prompt(self, ctx) -> str | None:
        return ctx.state.get("memory_block")

    # ------------------------------------------------------------------
    # Per-cycle: scope-bound tools + pressure-ingest post_turn hook
    # ------------------------------------------------------------------

    def _scope_bound(self, fn, cycle):
        """Wrap a memory tool so it runs inside the cycle's visible scope, while
        preserving the tool's name/docstring/signature for schema extraction."""
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with self._tools.scope_context(self._resolve_ctx_scopes(cycle.context)):
                return await fn(*args, **kwargs)
        wrapper.__signature__ = inspect.signature(fn)
        return wrapper

    @hook(HookType.TURN_START)
    def wire_tools(self, cycle) -> None:
        if self._runner is None:
            logger.error("[memory] TURN_START before STARTUP — skipping")
            return

        cycle.tool_handler.register_tool(self._scope_bound(self._tools.search_memory, cycle),
                                         always_on=True, required_permissions={Permission.MEMORY_READ})
        cycle.tool_handler.register_tool(self._scope_bound(self._tools.memory_stats, cycle),
                                         always_on=False, required_permissions={Permission.MEMORY_READ})

        # pressure ingest
        librarian_cfg = self.config.get("librarian", {})
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
                self._runner.queue.put_nowait({"type": "branch", "tail_node_id": final_tail})
            cycle.db.set_state(final_tail, "memory_tokens_since_ingest", tokens_since)

        cycle.post_turn_hooks.append(_pressure_hook)

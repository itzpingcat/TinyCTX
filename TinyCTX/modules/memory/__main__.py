"""
modules/memory/__main__.py

Wires the memory module into the agent. Uses the new two-function API:

  register_runtime(runtime) — singletons and tool registration at startup
  register_agent(cycle)     — hook + prompt wiring per AgentCycle

Singletons (store, indexer, embedder) are created in register_runtime and
captured by register_agent via closure. Each AgentCycle gets its own fresh
per-turn ephemeral state (results list) — nothing is shared between concurrent cycles.

See original docstring for full feature description.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
from pathlib import Path

from TinyCTX.context import HOOK_PRE_ASSEMBLE_ASYNC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_file(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception as exc:
        logger.warning("[memory] could not read %s: %s", path, exc)
        return None


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _format_results(results: list[dict], budget_tokens: int) -> str | None:
    if not results:
        return None

    header   = "<memory>"
    footer   = "</memory>"
    overhead = _estimate_tokens(header + "\n\n" + footer)

    blocks:      list[str] = []
    used_tokens: int       = overhead
    dropped:     int       = 0

    for i, r in enumerate(results):
        block = f"[{r['file']}]\n{r['text'].strip()}"
        cost  = _estimate_tokens(block + "\n\n")

        if i > 0 and budget_tokens > 0 and used_tokens + cost > budget_tokens:
            dropped += 1
            continue

        blocks.append(block)
        used_tokens += cost

    parts = [header] + blocks + [footer]
    if dropped:
        parts.insert(-1, f"[{dropped} chunk(s) omitted — memory budget reached]")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# register_runtime() — called once at startup
# ---------------------------------------------------------------------------

def register_runtime(runtime) -> None:
    workspace = Path(runtime.config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        from TinyCTX.modules.memory import EXTENSION_META
        defaults: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        defaults = {}

    overrides: dict = {}
    if hasattr(runtime.config, "extra") and isinstance(runtime.config.extra, dict):
        overrides = runtime.config.extra.get("memory_search", {})

    cfg: dict = {**defaults, **overrides}

    def _resolve(filename: str) -> Path:
        p = Path(filename)
        return p if p.is_absolute() else workspace / p

    budget_tokens = int(cfg["memory_budget_tokens"])

    soul_path   = _resolve(cfg["soul_file"])
    agents_path = _resolve(cfg["agents_file"])
    memory_path = _resolve(cfg["memory_file"])
    tools_path  = _resolve(cfg["tools_file"])

    from TinyCTX.modules.memory.inject import MacroResolver, make_provider
    resolver = MacroResolver()

    memory_dir = _resolve(cfg["memory_dir"])
    db_path    = _resolve(cfg["db_file"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    top_k               = int(cfg["top_k"])
    bm25_weight         = float(cfg["bm25_weight"])
    decay_halflife_days = float(cfg.get("decay_halflife_days", 30.0))
    decay_weight        = float(cfg.get("decay_weight", 0.0))
    auto_inject         = bool(cfg["auto_inject"])

    from TinyCTX.modules.memory.chunkers import get_strategy
    chunk_kwargs: dict = cfg.get("chunk_kwargs") or {}
    strategy = get_strategy(cfg["chunk_strategy"], **chunk_kwargs)

    from TinyCTX.modules.memory.store import MemoryStore
    store = MemoryStore(db_path)
    atexit.register(store.close)

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
                "[memory] embedding_model '%s' not usable (%s) — falling back to BM25 only",
                embedding_model, exc,
            )

    model_name_str = (
        runtime.config.models[embedding_model].model
        if embedding_model and embedding_model in runtime.config.models
        else ""
    )

    from TinyCTX.modules.memory.indexer import MemoryIndexer
    indexer = MemoryIndexer(
        store           = store,
        memory_dir      = memory_dir,
        strategy        = strategy,
        embedder        = embedder,
        embedding_model = model_name_str,
    )

    # ------------------------------------------------------------------
    # Background consolidation hook (post-turn, fires per cycle)
    # ------------------------------------------------------------------
    nudge_threshold = float(cfg.get("nudge_threshold", 0.80))
    nudge_message   = cfg.get("nudge_message", "")
    token_limit     = runtime.config.context

    if nudge_threshold > 0.0 and nudge_message:
        nudge_delta = int(nudge_threshold * token_limit)

        async def _consolidation_hook(tail_node_id: str) -> None:
            # We don't have ctx.state here — use the DB to get a fresh token count.
            # As a practical heuristic, we check if nudge_delta tokens have elapsed
            # by reading the session state from the DB.
            state, _ = runtime.db.load_session_state(tail_node_id)
            tokens_now      = int(state.get("tokens_used", 0) or 0)
            tokens_at_nudge = int(state.get("memory_nudge_tokens_at_last", 0) or 0)

            if tokens_now - tokens_at_nudge < nudge_delta:
                return

            import datetime
            date_str = datetime.date.today().strftime("%d-%m-%Y")
            msg_text = nudge_message.format(date=date_str)

            from TinyCTX.contracts import InboundMessage, ContentType, UserIdentity, Platform
            import time as _time

            opening = runtime.db.add_node(
                parent_id=tail_node_id,
                role="user",
                content=msg_text,
            )

            synth_msg = InboundMessage(
                tail_node_id=opening.id,
                author=UserIdentity(
                    platform=Platform.SYSTEM,
                    user_id="system",
                    username="system",
                ),
                content_type=ContentType.TEXT,
                text=msg_text,
                message_id=f"consolidation-{_time.time_ns()}",
                timestamp=_time.time(),
                trigger=True,
                permission_level=100,
            )
            await runtime.push(synth_msg)

            # Update nudge timestamp in state
            delta = {"memory_nudge_tokens_at_last": tokens_now}
            runtime.db.update_node_state_delta(tail_node_id, __import__('json').dumps(delta))

            logger.info(
                "[memory] background consolidation spawned off tail=%s "
                "(delta %d/%d tokens since last nudge)",
                tail_node_id, tokens_now - tokens_at_nudge, nudge_delta,
            )

        runtime.register_background_hook(_consolidation_hook)
        logger.info(
            "[memory] background consolidation enabled — threshold %.0f%% delta (%d tokens)",
            nudge_threshold * 100, nudge_delta,
        )
    else:
        logger.info("[memory] background consolidation disabled")

    # ------------------------------------------------------------------
    # memory_search tool — registered once on the runtime-level tool_handler
    # template (for per-cycle inheritance via ModuleRegistry)
    # ------------------------------------------------------------------

    async def memory_search(query: str) -> str:
        """
        Search the memory store for information relevant to a query.
        Use this to explicitly recall facts, notes, or context that may
        not have been automatically injected into the current turn.

        Args:
            query: The topic, question, or keywords to search for.
        """
        await indexer.sync()

        q_vec = None
        if embedder is not None:
            try:
                q_vec = await embedder.embed_one(query)
            except Exception as exc:
                logger.warning("[memory] tool query embedding failed: %s — using BM25 only", exc)

        results = store.hybrid_search(
            query, q_vec, top_k, bm25_weight,
            decay_halflife_days=decay_halflife_days,
            decay_weight=decay_weight,
        )
        if not results:
            return "[no memory found for that query]"

        formatted = _format_results(results, budget_tokens)
        if formatted is None:
            return "[no memory found for that query]"
        return formatted

    _ms_vis = str(
        cfg.get("tools", {}).get("memory_search", "always_on")
    ).lower().strip()

    # ------------------------------------------------------------------
    # /memory consolidate command
    # ------------------------------------------------------------------
    if nudge_message:
        async def _cmd_consolidate(args: list[str], context: dict) -> None:
            console = context.get("console")
            c       = context.get("theme_c", lambda k: "")
            tail    = context.get("tail_node_id")
            if not tail:
                if console:
                    console.print(f"[{c('error')}]  ✗  memory: no active session to consolidate[/{c('error')}]")
                return

            import datetime, time as _time
            from TinyCTX.contracts import InboundMessage, ContentType, UserIdentity, Platform
            date_str = datetime.date.today().strftime("%d-%m-%Y")
            msg_text = nudge_message.format(date=date_str)

            opening = runtime.db.add_node(parent_id=tail, role="user", content=msg_text)
            synth_msg = InboundMessage(
                tail_node_id=opening.id,
                author=UserIdentity(platform=Platform.SYSTEM, user_id="system", username="system"),
                content_type=ContentType.TEXT,
                text=msg_text,
                message_id=f"consolidation-cmd-{_time.time_ns()}",
                timestamp=_time.time(),
                trigger=True,
                permission_level=100,
            )
            await runtime.push(synth_msg)
            if console:
                console.print(f"[{c('tool_ok')}]  ✓  memory consolidation started (branch off tail={tail[:8]}…)[/{c('tool_ok')}]")
            logger.info("[memory] /memory consolidate — branch fired off tail=%s", tail)

        runtime.commands.register(
            "memory", "consolidate", _cmd_consolidate,
            help="Spawn a memory consolidation branch immediately",
        )

    # Store singletons for per-cycle wiring via closure
    _singletons = dict(
        store=store, indexer=indexer, embedder=embedder,
        cfg=cfg, budget_tokens=budget_tokens, top_k=top_k,
        bm25_weight=bm25_weight, decay_halflife_days=decay_halflife_days,
        decay_weight=decay_weight, auto_inject=auto_inject,
        soul_path=soul_path, agents_path=agents_path,
        memory_path=memory_path, tools_path=tools_path,
        resolver=resolver, ms_vis=_ms_vis,
        workspace=workspace,
        memory_search_fn=memory_search,
    )

    # Attach register_agent as module-level so ModuleRegistry can find it
    import sys as _sys
    _this = _sys.modules[__name__]

    def _register_agent(cycle) -> None:
        _wire_agent(cycle, **_singletons)

    # Replace the global register_agent function at module level
    _this.register_agent = _register_agent

    logger.info(
        "[memory] ready — dir: %s | db: %s | strategy: %s | embedder: %s | auto_inject: %s",
        memory_dir, db_path, cfg["chunk_strategy"],
        model_name_str or "BM25 only", auto_inject,
    )


# ---------------------------------------------------------------------------
# _wire_agent() — per-cycle wiring (called via register_agent)
# ---------------------------------------------------------------------------

def _wire_agent(
    cycle,
    store, indexer, embedder,
    cfg, budget_tokens, top_k, bm25_weight,
    decay_halflife_days, decay_weight, auto_inject,
    soul_path, agents_path, memory_path, tools_path,
    resolver, ms_vis, workspace,
    memory_search_fn,
) -> None:
    from TinyCTX.modules.memory.inject import make_provider

    # 1. Static prompt providers
    cycle.context.register_prompt(
        "soul",
        make_provider(soul_path, workspace, extra_macros=resolver),
        role="system",
        priority=int(cfg["soul_priority"]),
    )
    cycle.context.register_prompt(
        "agents",
        make_provider(agents_path, workspace, extra_macros=resolver),
        role="system",
        priority=int(cfg["agents_priority"]),
    )
    cycle.context.register_prompt(
        "memory",
        make_provider(memory_path, workspace, extra_macros=resolver),
        role="system",
        priority=int(cfg["memory_priority"]),
    )
    cycle.context.register_prompt(
        "tools",
        make_provider(tools_path, workspace, extra_macros=resolver),
        role="system",
        priority=int(cfg["tools_priority"]),
    )

    # 2. Async pre-assemble hook — ephemeral results shared via closure
    results: list = []

    async def _pre_assemble_async(ctx) -> None:
        if ctx.dialogue:
            last_role = ctx.dialogue[-1].role
            if last_role in ("tool", "assistant"):
                return

        await indexer.sync()

        query = ""
        for entry in reversed(ctx.dialogue):
            if entry.role == "user":
                content = entry.content
                if isinstance(content, list):
                    query = " ".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ).strip()
                else:
                    query = content
                if query.strip():
                    break

        if not query.strip():
            results[:] = []
            ctx.state["memory_search_results"] = []
            return

        if budget_tokens > 0:
            total_tokens = store.total_chunks_text_tokens()
            if total_tokens <= budget_tokens:
                if total_tokens > 0:
                    found = store.hybrid_search(
                        query, None, top_k=999, bm25_weight=1.0,
                        decay_halflife_days=decay_halflife_days,
                        decay_weight=decay_weight,
                    )
                    results[:] = found
                    ctx.state["memory_search_results"] = found
                else:
                    results[:] = []
                    ctx.state["memory_search_results"] = []
                return

        q_vec = None
        if embedder is not None:
            try:
                q_vec = await embedder.embed_one(query)
            except Exception as exc:
                logger.warning("[memory] query embedding failed: %s — using BM25 only", exc)

        found = store.hybrid_search(
            query, q_vec, top_k, bm25_weight,
            decay_halflife_days=decay_halflife_days,
            decay_weight=decay_weight,
        )
        results[:] = found
        ctx.state["memory_search_results"] = found

        if found:
            logger.debug(
                "[memory] search '%s…' → %d result(s) (top score %.3f)",
                query[:40], len(found), found[0]["score"],
            )

    cycle.context.register_hook(
        HOOK_PRE_ASSEMBLE_ASYNC,
        _pre_assemble_async,
        priority=0,
    )

    # 3. Auto-inject prompt
    if auto_inject:
        cycle.context.register_prompt(
            "memory_search",
            lambda ctx: _format_results(
                ctx.state.get("memory_search_results", results),
                budget_tokens,
            ),
            role="system",
            priority=int(cfg["search_priority"]),
        )

    # 4. memory_search tool
    if ms_vis != "disabled":
        cycle.tool_handler.register_tool(
            memory_search_fn,
            always_on=(ms_vis != "deferred"),
            min_permission=25,
        )


# ---------------------------------------------------------------------------
# register_agent — populated by register_runtime; placeholder before that
# ---------------------------------------------------------------------------

def register_agent(cycle) -> None:
    """Populated by register_runtime. No-op if called before startup."""
    pass


# ---------------------------------------------------------------------------
# Backward-compat: register_agent(agent) shim
# ---------------------------------------------------------------------------

def register_agent(agent) -> None:
    """Legacy shim — called by old code that passes an agent-like object."""
    from TinyCTX.runtime import Runtime as _Runtime
    if isinstance(agent, _Runtime):
        register_runtime(agent)
    else:
        # Per-cycle call — use register_agent if it's been populated
        register_agent(agent)

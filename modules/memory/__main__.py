"""
modules/memory/__main__.py

Wires the memory module into the agent. Does three things:

1. Static prompt providers
   SOUL.md, AGENTS.md, MEMORY.md are read fresh on every assemble() and
   injected as system prompt blocks at their configured priorities.

2. Async pre-assemble hook  (HOOK_PRE_ASSEMBLE_ASYNC)
   Before each turn's assemble():
     a. MemoryIndexer.sync() — re-indexes any dirty *.md files under
        workspace/memory/ (lazy, no-op when everything is current).
     b. Embeds the last user message (if embedder configured).
     c. Runs hybrid_search(query, query_vector, top_k, bm25_weight).
     d. Stores results in ctx.state["memory_search_results"].

   If auto_inject is True, a prompt provider reads the results, applies the
   memory budget, and injects them as a <memory> block at search_priority.
   If auto_inject is False, results are only available via the tool.

3. memory_search tool
   Always registered regardless of auto_inject. The agent calls this
   explicitly when it needs to recall something with a specific query,
   or when auto_inject is False. The tool also applies the memory budget
   so its output stays predictably sized.

Config lives under a top-level 'memory_search:' key in config.yaml
(or whatever extra key you choose — accessed via agent.config.extra).
All keys are optional; defaults are in modules/memory/__init__.py.

Convention: register(agent) — no imports from gateway, bridges, or contracts.
"""
from __future__ import annotations

import atexit
import logging
from pathlib import Path

from context import HOOK_PRE_ASSEMBLE_ASYNC, HistoryEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_file(path: Path) -> str | None:
    """Read a markdown file, return None if missing or empty."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception as exc:
        logger.warning("[memory] could not read %s: %s", path, exc)
        return None


def _estimate_tokens(text: str) -> int:
    """Fast character-based token estimate (1 token ≈ 4 chars)."""
    return len(text) // 4


def _format_results(results: list[dict], budget_tokens: int) -> str | None:
    """
    Format hybrid search results as a <memory> XML block, respecting the
    token budget.

    Chunks are included highest-score-first. The first (highest-scoring)
    chunk is always included regardless of budget — a zero-result block is
    never useful. Subsequent chunks are added until the next one would push
    over the budget, at which point they are dropped and a truncation note
    is appended. Set budget_tokens=0 to include all results unconditionally.
    """
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

        # Always include the first chunk; enforce budget from the second onward
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
# register()
# ---------------------------------------------------------------------------

def register(agent) -> None:
    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------
    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        from modules.memory import EXTENSION_META
        defaults: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        defaults = {}

    overrides: dict = {}
    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        overrides = agent.config.extra.get("memory_search", {})

    cfg: dict = {**defaults, **overrides}

    def _resolve(filename: str) -> Path:
        p = Path(filename)
        return p if p.is_absolute() else workspace / p

    budget_tokens = int(cfg["memory_budget_tokens"])

    # ------------------------------------------------------------------
    # 1. Static prompt providers (SOUL / AGENTS / MEMORY)
    # ------------------------------------------------------------------
    soul_path   = _resolve(cfg["soul_file"])
    agents_path = _resolve(cfg["agents_file"])
    memory_path = _resolve(cfg["memory_file"])

    # Build macro resolver for injected files. Built-ins ({date}, {datetime},
    # {workspace}) are handled by make_provider automatically.
    from modules.memory.inject import MacroResolver, make_provider

    resolver = MacroResolver()

    agent.context.register_prompt(
        "soul",
        make_provider(soul_path, workspace, extra_macros=resolver),
        role="system",
        priority=int(cfg["soul_priority"]),
    )
    agent.context.register_prompt(
        "agents",
        make_provider(agents_path, workspace, extra_macros=resolver),
        role="system",
        priority=int(cfg["agents_priority"]),
    )
    agent.context.register_prompt(
        "memory",
        make_provider(memory_path, workspace, extra_macros=resolver),
        role="system",
        priority=int(cfg["memory_priority"]),
    )

    logger.info(
        "[memory] static providers — soul: %s | agents: %s | memory: %s",
        soul_path, agents_path, memory_path,
    )

    # ------------------------------------------------------------------
    # 2. Search index setup
    # ------------------------------------------------------------------
    memory_dir = _resolve(cfg["memory_dir"])
    db_path    = _resolve(cfg["db_file"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    top_k               = int(cfg["top_k"])
    bm25_weight         = float(cfg["bm25_weight"])
    decay_halflife_days = float(cfg.get("decay_halflife_days", 30.0))
    decay_weight        = float(cfg.get("decay_weight", 0.0))
    auto_inject         = bool(cfg["auto_inject"])

    from modules.memory.chunkers import get_strategy
    chunk_kwargs: dict = cfg.get("chunk_kwargs") or {}
    strategy = get_strategy(cfg["chunk_strategy"], **chunk_kwargs)

    from modules.memory.store import MemoryStore
    store = MemoryStore(db_path)
    atexit.register(store.close)

    embedder        = None
    embedding_model = cfg.get("embedding_model", "").strip()

    if embedding_model:
        try:
            from ai import Embedder
            emb_cfg  = agent.config.get_embedding_model(embedding_model)
            embedder = Embedder.from_config(emb_cfg)
            logger.info("[memory] embedder: %s @ %s", emb_cfg.model, emb_cfg.base_url)
        except (KeyError, ValueError) as exc:
            logger.warning(
                "[memory] embedding_model '%s' not usable (%s) — falling back to BM25 only",
                embedding_model, exc,
            )

    model_name_str = (
        agent.config.models[embedding_model].model
        if embedding_model and embedding_model in agent.config.models
        else ""
    )

    from modules.memory.indexer import MemoryIndexer
    indexer = MemoryIndexer(
        store           = store,
        memory_dir      = memory_dir,
        strategy        = strategy,
        embedder        = embedder,
        embedding_model = model_name_str,
    )

    # ------------------------------------------------------------------
    # 3. Async pre-assemble hook
    # ------------------------------------------------------------------

    async def _pre_assemble_async(ctx) -> None:
        await indexer.sync()

        query = ""
        for entry in reversed(ctx.dialogue):
            if entry.role == "user":
                query = entry.content
                break

        if not query.strip():
            ctx.state["memory_search_results"] = []
            return

        query_vector = None
        if embedder is not None:
            try:
                query_vector = await embedder.embed_one(query)
            except Exception as exc:
                logger.warning("[memory] query embedding failed: %s — using BM25 only", exc)

        results = store.hybrid_search(
            query, query_vector, top_k, bm25_weight,
            decay_halflife_days=decay_halflife_days,
            decay_weight=decay_weight,
        )
        ctx.state["memory_search_results"] = results

        if results:
            logger.debug(
                "[memory] search '%s…' → %d result(s) (top score %.3f)",
                query[:40], len(results), results[0]["score"],
            )

    agent.context.register_hook(
        HOOK_PRE_ASSEMBLE_ASYNC,
        _pre_assemble_async,
        priority=0,
    )

    # ------------------------------------------------------------------
    # 3b. Context nudge — recurs on delta, not absolute fill
    # ------------------------------------------------------------------

    nudge_threshold = float(cfg.get("nudge_threshold", 0.80))
    nudge_message   = cfg.get("nudge_message", "")

    if nudge_threshold > 0.0 and nudge_message:
        token_limit = agent.config.context
        nudge_delta = int(nudge_threshold * token_limit)

        async def _nudge_hook(ctx) -> None:
            tokens_now      = ctx.state.get("tokens_used", 0)
            tokens_at_nudge = ctx.state.get("memory_nudge_tokens_at_last", 0)

            if tokens_now - tokens_at_nudge >= nudge_delta:
                import datetime
                date_str = datetime.date.today().strftime("%d-%m-%Y")
                msg = nudge_message.format(date=date_str)
                ctx.add(HistoryEntry.user(msg))
                ctx.state["memory_nudge_tokens_at_last"] = tokens_now
                logger.info(
                    "[memory] nudge injected (delta %d/%d tokens since last nudge)",
                    tokens_now - tokens_at_nudge, nudge_delta,
                )

        agent.context.register_hook(
            HOOK_PRE_ASSEMBLE_ASYNC,
            _nudge_hook,
            priority=100,  # run after search (priority=0)
        )
        logger.info(
            "[memory] context nudge enabled — threshold %.0f%% delta (%d tokens)",
            nudge_threshold * 100, nudge_delta,
        )
    else:
        logger.info("[memory] context nudge disabled")

    # ------------------------------------------------------------------
    # 4. Auto-inject prompt provider
    # ------------------------------------------------------------------

    if auto_inject:
        agent.context.register_prompt(
            "memory_search",
            lambda ctx: _format_results(
                ctx.state.get("memory_search_results", []),
                budget_tokens,
            ),
            role="system",
            priority=int(cfg["search_priority"]),
        )
        budget_note = f"{budget_tokens} tokens" if budget_tokens > 0 else "unlimited"
        logger.info(
            "[memory] auto_inject enabled — priority %d, budget %s",
            cfg["search_priority"], budget_note,
        )
    else:
        logger.info("[memory] auto_inject disabled — retrieval via memory_search tool only")

    # ------------------------------------------------------------------
    # 5. memory_search tool
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
                logger.warning("[memory] tool query embedding failed: %s", exc)

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

    # Default: always_on. Override via config.yaml: memory_search.tools.memory_search: deferred|disabled
    _ms_vis = str(
        cfg.get("tools", {}).get("memory_search", "always_on")
    ).lower().strip()
    if _ms_vis != "disabled":
        agent.tool_handler.register_tool(memory_search, always_on=(_ms_vis != "deferred"))

    logger.info(
        "[memory] ready — dir: %s | db: %s | strategy: %s | embedder: %s | auto_inject: %s",
        memory_dir,
        db_path,
        cfg["chunk_strategy"],
        model_name_str or "BM25 only",
        auto_inject,
    )

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

import logging
from pathlib import Path

from context import HOOK_PRE_ASSEMBLE_ASYNC

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

    Chunks are included highest-score-first. Once adding the next chunk's
    formatted block would exceed budget_tokens, it and all remaining chunks
    are dropped and a truncation note is appended. Set budget_tokens=0 to
    include all results unconditionally.
    """
    if not results:
        return None

    header   = "<memory>"
    footer   = "</memory>"
    overhead = _estimate_tokens(header + "\n\n" + footer)

    blocks:        list[str] = []
    used_tokens:   int       = overhead
    dropped:       int       = 0

    for r in results:
        block = f"[{r['file']}]\n{r['text'].strip()}"
        cost  = _estimate_tokens(block + "\n\n")

        if budget_tokens > 0 and used_tokens + cost > budget_tokens:
            dropped += 1
            continue

        blocks.append(block)
        used_tokens += cost

    if not blocks:
        return None

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

    # Module-level overrides live under a top-level extra key in config.yaml.
    # The conventional key is "memory_search" to avoid collision with the
    # core "workspace" block; use agent.config.extra.get("memory_search", {}).
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

    agent.context.register_prompt(
        "soul",
        lambda _ctx: _read_file(soul_path),
        role="system",
        priority=int(cfg["soul_priority"]),
    )
    agent.context.register_prompt(
        "agents",
        lambda _ctx: _read_file(agents_path),
        role="system",
        priority=int(cfg["agents_priority"]),
    )
    agent.context.register_prompt(
        "memory",
        lambda _ctx: _read_file(memory_path),
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

    top_k       = int(cfg["top_k"])
    bm25_weight = float(cfg["bm25_weight"])
    auto_inject = bool(cfg["auto_inject"])

    # Chunking strategy
    from modules.memory.chunkers import get_strategy
    chunk_kwargs: dict = cfg.get("chunk_kwargs") or {}
    strategy = get_strategy(cfg["chunk_strategy"], **chunk_kwargs)

    # Store
    from modules.memory.store import MemoryStore
    store = MemoryStore(db_path)

    # Embedder (optional)
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

    # Indexer
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
        # a. Sync index (lazy — no-op if nothing is dirty)
        await indexer.sync()

        # b. Extract last user message as search query
        query = ""
        for entry in reversed(ctx.dialogue):
            if entry.role == "user":
                query = entry.content
                break

        if not query.strip():
            ctx.state["memory_search_results"] = []
            return

        # c. Embed query (None → BM25-only path in hybrid_search)
        query_vector = None
        if embedder is not None:
            try:
                query_vector = await embedder.embed_one(query)
            except Exception as exc:
                logger.warning("[memory] query embedding failed: %s — using BM25 only", exc)

        # d. Hybrid search → store in ctx.state for prompt provider + tool
        results = store.hybrid_search(query, query_vector, top_k, bm25_weight)
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

        results = store.hybrid_search(query, q_vec, top_k, bm25_weight)
        if not results:
            return "[no memory found for that query]"

        # Apply budget — tool output shouldn't be unbounded either
        formatted = _format_results(results, budget_tokens)
        if formatted is None:
            return "[no memory found for that query]"
        return formatted

    agent.tool_handler.register_tool(memory_search)

    logger.info(
        "[memory] ready — dir: %s | db: %s | strategy: %s | embedder: %s | auto_inject: %s",
        memory_dir,
        db_path,
        cfg["chunk_strategy"],
        model_name_str or "BM25 only",
        auto_inject,
    )

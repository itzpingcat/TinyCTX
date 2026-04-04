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

4. Background consolidation hook  (Phase 3)
   When the context token fill crosses nudge_threshold * token_limit (delta
   since the last nudge), a background branch is spawned off the current tail:
     - An opening node is created via db.add_node() with the nudge_message.
     - A new AgentLoop is started on that node as a detached asyncio task.
     - The current conversation continues uninterrupted.
   The background loop walks its own ancestor chain, calls memory write tools,
   and exits when done. The user's cursor is never moved.

Config lives under a top-level 'memory_search:' key in config.yaml
(or whatever extra key you choose — accessed via agent.config.extra).
All keys are optional; defaults are in modules/memory/__init__.py.

Convention: register(agent) — no imports from gateway, bridges, or contracts.
"""
from __future__ import annotations

import asyncio
import atexit
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
        # Optimization 1: Only search on user-message turns, not tool-call cycles.
        # The last entry in dialogue is the trigger: if it's a tool result (or
        # assistant turn), we're mid-tool-loop — reuse the cached results instead.
        if ctx.dialogue:
            last_role = ctx.dialogue[-1].role
            if last_role in ("tool", "assistant"):
                # Preserve whatever was found on the first (user) cycle.
                return

        await indexer.sync()

        query = ""
        for entry in reversed(ctx.dialogue):
            if entry.role == "user":
                content = entry.content
                # content may be a list[dict] when the user message contains
                # image blocks (e.g. the synthetic vision injection). Extract
                # only text parts for the search query.
                if isinstance(content, list):
                    query = " ".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ).strip()
                else:
                    query = content
                # Skip the synthetic image-injection turn (no useful text)
                # and keep looking for the real user message.
                if query.strip():
                    break

        if not query.strip():
            ctx.state["memory_search_results"] = []
            return

        # Optimization 2: If the total text of all stored chunks fits within
        # the memory budget already, skip the embedding round-trip entirely —
        # auto_inject will just return everything via _format_results anyway.
        if budget_tokens > 0:
            total_tokens = store.total_chunks_text_tokens()
            if total_tokens <= budget_tokens:
                # Fetch all chunks cheaply via BM25 with a broad wildcard,
                # but only if there's anything stored at all.
                if total_tokens > 0:
                    results = store.hybrid_search(
                        query, None, top_k=999, bm25_weight=1.0,
                        decay_halflife_days=decay_halflife_days,
                        decay_weight=decay_weight,
                    )
                    ctx.state["memory_search_results"] = results
                    logger.debug(
                        "[memory] all chunks fit in budget (%d tokens) — skipped embeddings, returned %d chunk(s)",
                        total_tokens, len(results),
                    )
                else:
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
    # 3b. Background consolidation hook  (Phase 3)
    #
    # Replaces the old inline nudge. Instead of injecting a summarization
    # message into the live conversation, we spawn a background branch off
    # the current tail when the token delta since the last nudge exceeds the
    # threshold. The background AgentLoop runs unobserved, calls memory write
    # tools, and exits. The user's cursor is never moved.
    # ------------------------------------------------------------------

    nudge_threshold = float(cfg.get("nudge_threshold", 0.80))
    nudge_message   = cfg.get("nudge_message", "")

    if nudge_threshold > 0.0 and nudge_message:
        token_limit = agent.config.context
        nudge_delta = int(nudge_threshold * token_limit)

        async def _consolidation_hook(tail_node_id: str, config) -> None:
            tokens_now      = agent.context.state.get("tokens_used", 0)
            tokens_at_nudge = agent.context.state.get("memory_nudge_tokens_at_last", 0)
            current_limit   = int(getattr(config, "context", 0) or 0)
            current_delta   = int(nudge_threshold * current_limit) if current_limit > 0 else nudge_delta

            if tokens_now - tokens_at_nudge < current_delta:
                return

            import datetime
            from context import HistoryEntry
            date_str = datetime.date.today().strftime("%d-%m-%Y")
            msg = nudge_message.format(date=date_str)

            # Use agent._db if present (real AgentLoop), otherwise fall back
            # to the DB wired directly into the context (e.g. _FakeAgent tests).
            db = getattr(agent, "_db", None) or getattr(agent.context, "_db", None)
            ctx_tail = agent.context.tail_node_id
            if db is not None and ctx_tail is not None:
                opening = db.add_node(
                    parent_id=tail_node_id,
                    role="user",
                    content=msg,
                )
                from agent import _run_background
                asyncio.create_task(_run_background(opening.id, config))
            else:
                # No-DB (legacy/test) path: inject the nudge inline so the agent
                # sees it on the next assemble().
                agent.context.dialogue.append(HistoryEntry.user(msg))

            agent.context.state["memory_nudge_tokens_at_last"] = tokens_now
            logger.info(
                "[memory] background consolidation spawned off tail=%s "
                "(delta %d/%d tokens since last nudge)",
                tail_node_id, tokens_now - tokens_at_nudge, current_delta,
            )

        agent.register_background_hook(_consolidation_hook)
        logger.info(
            "[memory] background consolidation enabled — threshold %.0f%% delta (%d tokens)",
            nudge_threshold * 100, nudge_delta,
        )
    else:
        logger.info("[memory] background consolidation disabled")

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

    # ------------------------------------------------------------------
    # 6. /memory consolidate command
    #
    # Immediately spawns a background consolidation branch off the current
    # tail, regardless of the nudge_threshold. Works even when background
    # consolidation is disabled in config (nudge_threshold=0).
    # ------------------------------------------------------------------
    registry = getattr(agent, "commands", None)
    if registry is not None and nudge_message:
        async def _cmd_consolidate(args: list[str], context: dict) -> None:
            console = context.get("console")
            c       = context.get("theme_c", lambda k: "")

            db = getattr(agent, "_db", None) or getattr(agent.context, "_db", None)
            tail = agent.context.tail_node_id

            if db is None or tail is None:
                if console:
                    console.print(f"[{c('error')}]  ✗  memory: no active session to consolidate[/{c('error')}]")
                return

            import datetime
            date_str = datetime.date.today().strftime("%d-%m-%Y")
            msg      = nudge_message.format(date=date_str)

            opening = db.add_node(parent_id=tail, role="user", content=msg)

            # Fire directly — queue_background_branch() only drains inside
            # AgentLoop.run(), which never runs for a slash command handler.
            from agent import _run_background
            asyncio.create_task(_run_background(opening.id, agent.config))

            if console:
                console.print(f"[{c('tool_ok')}]  ✓  memory consolidation started (branch off tail={tail[:8]}…)[/{c('tool_ok')}]")
            logger.info("[memory] /memory consolidate — branch fired off tail=%s", tail)

        registry.register(
            "memory", "consolidate", _cmd_consolidate,
            help="Spawn a memory consolidation branch immediately",
        )
        logger.debug("[memory] registered /memory consolidate command")

    logger.info(
        "[memory] ready — dir: %s | db: %s | strategy: %s | embedder: %s | auto_inject: %s",
        memory_dir,
        db_path,
        cfg["chunk_strategy"],
        model_name_str or "BM25 only",
        auto_inject,
    )

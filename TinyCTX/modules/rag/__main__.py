"""
modules/rag/__main__.py

RAG module wiring: databank discovery, per-databank indexing, rag_search tool,
set_auto_rag_databanks tool, and auto-inject system prompt block.

Architecture
------------
One DataStore + DataBankIndexer pair per discovered databank. Singletons are
created once on the first register_agent call and shared across all cycles.

Auto-rag state is stored in session state under the key "rag_auto_targets"
(a list of databank name strings). set_auto_rag_databanks writes this key
via db.set_state (merge-write — safe alongside other modules' state on the
same node); the pre-assemble hook reads it each turn via db.get_state.

Databank layout (workspace/rag/):
    lore/            <- FilesDataBank "lore"
    characters/      <- FilesDataBank "characters"
    my_world.json    <- LoreBookDataBank "my_world"
    .cache/          <- SQLite DBs, one per databank (excluded from discovery)

Retrieval is dispatched through the DataBank protocol:
    rag_search tool   -> await bank.rag_search(query, store, embedder, top_k, bm25_weight)
    pre-assemble hook -> bank.auto_inject(text)  [synchronous]

Config is read from EXTENSION_META defaults merged with workspace overrides
under the "rag" key in the workspace config.

register_runtime is not used by this module (no commands or runtime hooks needed).
"""
from __future__ import annotations

import atexit
import logging
from pathlib import Path

from TinyCTX.context import HOOK_PRE_ASSEMBLE_ASYNC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton state — initialized once on first register_agent call
# ---------------------------------------------------------------------------

_initialized     = False
_stores:  dict   = {}   # name -> DataStore
_indexers: dict  = {}   # name -> DataBankIndexer
_databanks: dict = {}   # name -> DataBank
_embedder        = None
_cfg: dict       = {}
_workspace: Path | None = None
_strategy        = None
_model_name_str  = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _load_cfg(config) -> dict:
    """Merge EXTENSION_META defaults with workspace overrides under 'rag' key."""
    from TinyCTX.modules.rag import EXTENSION_META
    defaults: dict = EXTENSION_META.get("default_config", {})
    overrides: dict = {}
    if hasattr(config, "extra") and isinstance(config.extra, dict):
        overrides = config.extra.get("rag", {})
    return {**defaults, **overrides}


def _format_results(
    results: list[dict],
    budget_tokens: int,
    databank_name: str | None = None,
) -> str | None:
    """
    Format a list of search result dicts into a <rag_context> block.
    Each result dict: {file, path, text, score}.
    Returns None if results is empty.
    """
    if not results:
        return None

    label   = f" databank={databank_name!r}" if databank_name else ""
    header  = f"<rag_context{label}>"
    footer  = "</rag_context>"
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
        parts.insert(-1, f"[{dropped} chunk(s) omitted — result budget reached]")
    return "\n\n".join(parts)


async def _do_rag_search(
    name: str,
    query: str,
    top_k: int,
    bm25_weight: float,
) -> list[dict]:
    """Sync the indexer then dispatch to bank.rag_search. Returns [] on any error."""
    bank    = _databanks.get(name)
    store   = _stores.get(name)
    indexer = _indexers.get(name)
    if bank is None or store is None or indexer is None:
        return []
    try:
        await indexer.sync()
    except Exception as exc:
        logger.warning("[rag] sync failed for '%s': %s", name, exc)
        return []
    return await bank.rag_search(query, store, _embedder, top_k, bm25_weight)


# ---------------------------------------------------------------------------
# Singleton initialization
# ---------------------------------------------------------------------------

def _init_singletons(config) -> None:
    global _initialized, _embedder, _cfg, _workspace, _strategy, _model_name_str

    if _initialized:
        return

    workspace = Path(config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    _workspace = workspace

    _cfg = _load_cfg(config)

    cache_dir = _resolve_path(_cfg["cache_dir"], workspace)
    cache_dir.mkdir(parents=True, exist_ok=True)

    extensions: set[str] = {
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in _cfg.get("indexed_extensions", [".md", ".txt", ".rst"])
    }
    _cfg["_extensions"] = extensions  # stash for _sync_discovery

    # Embedder (optional)
    embedding_model = _cfg.get("embedding_model", "").strip()
    if embedding_model:
        try:
            from TinyCTX.ai import Embedder
            emb_cfg      = config.get_embedding_model(embedding_model)
            _embedder    = Embedder.from_config(emb_cfg)
            _model_name_str = (
                config.models[embedding_model].model
                if embedding_model in config.models
                else ""
            )
            logger.info("[rag] embedder: %s @ %s", emb_cfg.model, emb_cfg.base_url)
        except (KeyError, ValueError, AttributeError) as exc:
            logger.warning(
                "[rag] embedding_model '%s' not usable (%s) — BM25 only",
                embedding_model, exc,
            )

    # Chunking strategy
    from TinyCTX.modules.rag.chunkers import get_strategy
    chunk_kwargs: dict = _cfg.get("chunk_kwargs") or {}
    _strategy = get_strategy(_cfg["chunk_strategy"], **chunk_kwargs)

    _initialized = True
    logger.info(
        "[rag] ready — strategy: %s | embedder: %s",
        _cfg["chunk_strategy"], _model_name_str or "BM25 only",
    )

    # Initial discovery
    _sync_discovery()


def _resolve_path(rel: str, workspace: Path) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else workspace / p


def _sync_discovery() -> None:
    """Re-scan the rag directory and register any new databanks. Idempotent."""
    global _databanks, _stores, _indexers

    rag_dir   = _resolve_path(_cfg["rag_dir"], _workspace)
    cache_dir = _resolve_path(_cfg["cache_dir"], _workspace)
    extensions: set[str] = _cfg["_extensions"]

    from TinyCTX.modules.rag.databanks import discover_databanks
    from TinyCTX.modules.rag.store import DataStore
    from TinyCTX.modules.rag.indexer import DataBankIndexer

    current = discover_databanks(rag_dir, extensions)

    for name, bank in current.items():
        if name in _databanks:
            continue  # already registered
        db_path = cache_dir / f"{name}.db"
        store   = DataStore(db_path)
        indexer = DataBankIndexer(
            store           = store,
            databank        = bank,
            strategy        = _strategy,
            embedder        = _embedder,
            embedding_model = _model_name_str,
        )
        _databanks[name] = bank
        _stores[name]    = store
        _indexers[name]  = indexer
        atexit.register(store.close)
        logger.info("[rag] registered databank '%s' (%s)", name, bank.kind)

    removed = set(_databanks) - set(current)
    for name in removed:
        logger.info("[rag] databank '%s' removed from disk", name)
        _stores.pop(name, None)
        _indexers.pop(name, None)
        _databanks.pop(name, None)

    logger.debug("[rag] discovery complete — %d databank(s) active", len(_databanks))


# ---------------------------------------------------------------------------
# register_runtime — not used by this module
# ---------------------------------------------------------------------------

def register_runtime(runtime) -> None:
    pass


# ---------------------------------------------------------------------------
# register_agent — tool and hook wiring per cycle
# ---------------------------------------------------------------------------

def register_agent(cycle) -> None:
    _init_singletons(cycle.config)

    cfg          = _cfg
    top_k        = int(cfg["top_k"])
    bm25_weight  = float(cfg["bm25_weight"])
    budget_tokens = int(cfg["result_budget_tokens"])
    auto_priority = int(cfg["auto_inject_priority"])

    # Snapshot of auto-rag targets for this turn (populated in pre-assemble hook)
    auto_results_by_bank: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    # Pre-assemble hook: search auto-rag databanks, cache results for inject
    # ------------------------------------------------------------------

    async def _pre_assemble_async(ctx) -> None:
        auto_results_by_bank.clear()

        # Only run on user turns
        if ctx.dialogue and ctx.dialogue[-1].role in ("tool", "assistant"):
            return

        # Read auto-rag targets from session state
        targets: list[str] = cycle.db.get_state(ctx.tail_node_id, "rag_auto_targets") or []
        if not targets:
            return

        _sync_discovery()

        # Extract last user message text
        query = ""
        for entry in reversed(ctx.dialogue):
            if entry.role == "user":
                content = entry.content
                if isinstance(content, list):
                    query = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ).strip()
                else:
                    query = str(content)
                if query.strip():
                    break

        if not query.strip():
            return

        for name in targets:
            if name not in _databanks:
                logger.debug("[rag] auto-inject: unknown databank '%s'", name)
                continue

            bank = _databanks[name]
            try:
                results = bank.auto_inject(query)
            except Exception as exc:
                logger.warning("[rag] auto_inject failed for '%s': %s", name, exc)
                results = []

            if results:
                auto_results_by_bank[name] = results
                logger.debug("[rag] auto-inject '%s': %d result(s)", name, len(results))

    cycle.context.register_hook(HOOK_PRE_ASSEMBLE_ASYNC, _pre_assemble_async, priority=0)

    # ------------------------------------------------------------------
    # Auto-inject prompt block
    # ------------------------------------------------------------------

    def _auto_rag_prompt(_ctx) -> str:
        if not auto_results_by_bank:
            return ""
        parts = []
        for name, results in auto_results_by_bank.items():
            block = _format_results(results, budget_tokens, databank_name=name)
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    cycle.context.register_prompt(
        "rag_auto_inject",
        _auto_rag_prompt,
        role="system",
        priority=auto_priority,
    )

    # ------------------------------------------------------------------
    # Tool: rag_search
    # ------------------------------------------------------------------

    async def rag_search(query: str, targets: list, max_results: int = 0) -> str:
        """
        Search one or more databanks for information relevant to a query.

        Databank names come from the workspace/rag/ directory:
          - A subfolder named "lore" -> target name "lore"
          - A lorebook file named "Astraea.json" -> target name "Astraea"
        Use rag_list_databanks() first if you are unsure of the available names.

        Args:
            query:       The topic, question, or keywords to search for.
            targets:     List of databank name strings to search.
                         Example: ["Astraea"] or ["lore", "characters"].
                         Do NOT pass a generic word like "rag" — use the actual databank name.
            max_results: Maximum results to return per databank (0 = use module default).
        """
        if not isinstance(targets, list) or not targets:
            return "Error: targets must be a non-empty list of databank names"

        k = int(max_results) if max_results and int(max_results) > 0 else top_k
        _sync_discovery()

        unknown = [t for t in targets if t not in _stores]
        if unknown:
            available = sorted(_stores.keys()) or ["(none)"]
            return (
                f"Error: unknown databank(s) {unknown}. "
                f"Available: {available}"
            )

        all_parts: list[str] = []
        for name in targets:
            results = await _do_rag_search(name, query, k, bm25_weight)
            block   = _format_results(results, budget_tokens, databank_name=name)
            if block:
                all_parts.append(block)

        if not all_parts:
            return "No results found in the specified databank(s)"
        return "\n\n".join(all_parts)

    cycle.tool_handler.register_tool(rag_search, always_on=True, min_permission=25)

    # ------------------------------------------------------------------
    # Tool: set_auto_rag_databanks
    # ------------------------------------------------------------------

    def set_auto_rag_databanks(targets: list) -> str:
        """
        Set which databanks are automatically searched and injected into context each turn.
        Call with an empty list to disable auto-injection entirely.

        Databank names come from the workspace/rag/ directory:
          - A subfolder named "lore" -> target name "lore"
          - A lorebook file named "Astraea.json" -> target name "Astraea"
        Use rag_list_databanks() first if you are unsure of the available names.

        Args:
            targets: List of databank name strings to enable for auto-inject.
                     Example: ["Astraea"] or ["lore", "characters"].
                     Do NOT pass a generic word like "rag" — use the actual databank name.
                     Pass [] to clear all auto-inject databanks.
        """
        if not isinstance(targets, list):
            return "Error: targets must be a list"

        targets = [str(t) for t in targets]
        unknown = [t for t in targets if t and t not in _stores]
        if unknown:
            available = sorted(_stores.keys()) or ["(none)"]
            return (
                f"Error: unknown databank(s) {unknown}. "
                f"Available: {available}"
            )

        tail = cycle.context.tail_node_id
        cycle.db.set_state(tail, "rag_auto_targets", targets)

        if not targets:
            return "Auto-rag cleared — no databanks will be injected automatically"
        return f"Auto-rag set to: {targets}"

    cycle.tool_handler.register_tool(set_auto_rag_databanks, always_on=False, min_permission=25)

    # ------------------------------------------------------------------
    # Tool: rag_list_databanks
    # ------------------------------------------------------------------

    def rag_list_databanks() -> str:
        """
        List all available databanks and their types.

        Args: (none)
        """
        if not _databanks:
            return "No databanks found — add folders or worldinfo JSON files to workspace/rag/"
        lines = ["Available databanks:"]
        for name, bank in sorted(_databanks.items()):
            lines.append(f"  {name}  ({bank.kind})")
        return "\n".join(lines)

    cycle.tool_handler.register_tool(rag_list_databanks, always_on=False, min_permission=25)

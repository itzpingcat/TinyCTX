"""
modules/rag

RAG module wiring: databank discovery, per-databank indexing, rag_search tool,
set_auto_rag_databanks tool, and auto-inject system prompt block.

Architecture
------------
One DataStore + DataBankIndexer pair per discovered databank. Built once at
@hook(HookType.STARTUP) — process lifetime, shared by every AgentCycle.

Auto-rag state is stored in session state under the key "rag_auto_targets"
(a list of databank name strings). set_auto_rag_databanks writes this key
via db.set_state (merge-write — safe alongside other modules' state on the
same node); the pre-assemble hook reads it each turn via db.get_state, and
falls back to the "default_auto_targets" config list on branches where that
key was never written (see the hook for the None-vs-[] distinction).

Databank layout (workspace/rag/):
    lore/            <- FilesDataBank "lore"
    characters/      <- FilesDataBank "characters"
    my_world.json    <- legacy lorebook JSON, auto-converted to my_world/ on first discovery
    .cache/          <- SQLite DBs, one per databank (excluded from discovery)

FilesDataBank is the only databank kind. Retrieval:
    rag_search tool   -> await bank.rag_search(query, store, embedder, top_k, bm25_weight)
    pre-assemble hook -> await bank.auto_inject(text, store, embedder, top_k, bm25_weight)
                         — deterministic keyword-triggered lore matching, merged with
                         the same hybrid BM25+vector search rag_search uses.

set_auto_rag_databanks needs live cycle.context.tail_node_id/cycle.db, which
a @tool method has no way to receive — it's registered imperatively from
@hook(HookType.TURN_START), same pattern as modules/present. rag_search and
rag_list_databanks touch only this module's own process-lifetime state, so
they ARE plain @tool methods.

The auto-inject pre-assemble step does a real embed() network call, so it
must run as PRE_ASSEMBLE_ASYNC (awaited before assemble() proper, not the
synchronous PRE_ASSEMBLE Context.assemble() itself calls inline) — and since
Scratch doesn't exist yet at that point (it's created inside assemble()),
its results are cached on ctx.state (Context's own per-cycle-lifetime state
bag, same fix output_parser's nudge-budget state uses) for the paired
@prompt to read back out.
"""
from __future__ import annotations

import atexit
import logging
from pathlib import Path

from TinyCTX.decorators import hook, prompt, tool
from TinyCTX.hooks import HookType
from TinyCTX.module import Module
from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _resolve_path(rel: str, workspace: Path) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else workspace / p


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


class Rag(Module):
    """Databank retrieval: rag_search/set_auto_rag_databanks/rag_list_databanks
    tools, plus a keyword+hybrid-search auto-inject system prompt block."""

    settings = {
        "rag_dir":   {"default": "rag", "type": "str",
                      "description": "Directory under workspace containing named databank subdirectories."},
        "cache_dir": {"default": "rag/.cache", "type": "str",
                      "description": "Where per-databank SQLite index caches are stored."},
        "indexed_extensions": {"default": [".md", ".txt", ".rst"], "type": "list",
                                "description": "File extensions indexed from folder databanks."},
        "chunk_strategy": {"default": "markdown", "type": "select",
                            "options": {"markdown": "", "tokens": "", "chars": "", "delimiter": ""},
                            "description": "Chunking strategy name."},
        "chunk_kwargs": {"default": {}, "type": "dict", "description": "Kwargs passed through to get_strategy()."},
        "embedding_model": {"default": "", "type": "str",
                             "description": "Key from models: with kind: embedding, or empty for BM25-only mode."},
        "top_k": {"default": 5, "type": "int",
                  "description": "Default max chunks returned by rag_search when max_results is not specified."},
        "bm25_weight": {"default": 0.3, "type": "float",
                         "description": "BM25 share of hybrid score (vector weight = 1 - bm25_weight), fused via RRF."},
        "rrf_k": {"default": 60, "type": "int",
                  "description": "RRF's rank-damping constant — higher flattens the fusion curve."},
        "result_budget_tokens": {"default": 2048, "type": "int",
                                  "description": "Max tokens the formatted result block may occupy. 0 disables the budget."},
        "default_auto_targets": {"default": [], "type": "list",
                                  "description": "Databanks auto-searched every turn on branches that have "
                                                  "never called set_auto_rag_databanks."},
        "auto_inject_semantic": {"default": True, "type": "bool",
                                  "description": "Whether auto-inject also runs the hybrid BM25+vector search "
                                                  "(a real embed() network call per qualifying turn) in addition "
                                                  "to deterministic keyword/regex/constant firing."},
    }

    # @prompt's priority is fixed at class-definition time — see
    # modules/equipment_manifest's docstring for why. Matches this module's
    # own default auto_inject_priority.
    _AUTO_INJECT_PRIORITY = 25

    def __init__(self) -> None:
        self.stores:     dict = {}   # name -> DataStore
        self.indexers:   dict = {}   # name -> DataBankIndexer
        self.databanks:  dict = {}   # name -> FilesDataBank
        self.embedder:   object | None = None
        self.workspace:  Path | None = None
        self.strategy:   object | None = None
        self.model_name: str = ""

    # ------------------------------------------------------------------
    # STARTUP — singleton init, once per process
    # ------------------------------------------------------------------

    @hook(HookType.STARTUP)
    def load(self, runtime) -> None:
        config = runtime.config
        workspace = Path(config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace

        cache_dir = _resolve_path(self.config["cache_dir"], workspace)
        cache_dir.mkdir(parents=True, exist_ok=True)

        self._extensions: set[str] = {
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in self.config["indexed_extensions"]
        }

        embedding_model = self.config["embedding_model"].strip()
        if embedding_model:
            try:
                from TinyCTX.ai import Embedder
                emb_cfg          = config.get_embedding_model(embedding_model)
                self.embedder    = Embedder.from_config(emb_cfg)
                self.model_name  = (
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

        from TinyCTX.modules.rag.chunkers import get_strategy
        chunk_kwargs: dict = self.config["chunk_kwargs"] or {}
        self.strategy = get_strategy(self.config["chunk_strategy"], **chunk_kwargs)

        logger.info(
            "[rag] ready — strategy: %s | embedder: %s",
            self.config["chunk_strategy"], self.model_name or "BM25 only",
        )

        self._sync_discovery()

    def _sync_discovery(self) -> None:
        """Re-scan the rag directory and register any new databanks. Idempotent."""
        rag_dir   = _resolve_path(self.config["rag_dir"], self.workspace)
        cache_dir = _resolve_path(self.config["cache_dir"], self.workspace)

        from TinyCTX.modules.rag.databanks import discover_databanks
        from TinyCTX.modules.rag.store import DataStore
        from TinyCTX.modules.rag.indexer import DataBankIndexer

        current = discover_databanks(rag_dir, self._extensions)

        for name, bank in current.items():
            if name in self.databanks:
                continue  # already registered
            db_path = cache_dir / f"{name}.db"
            store   = DataStore(db_path)
            indexer = DataBankIndexer(
                store           = store,
                databank        = bank,
                strategy        = self.strategy,
                embedder        = self.embedder,
                embedding_model = self.model_name,
            )
            self.databanks[name] = bank
            self.stores[name]    = store
            self.indexers[name]  = indexer
            atexit.register(store.close)
            logger.info("[rag] registered databank '%s' (%s)", name, bank.kind)

        removed = set(self.databanks) - set(current)
        for name in removed:
            logger.info("[rag] databank '%s' removed from disk", name)
            self.stores.pop(name, None)
            self.indexers.pop(name, None)
            self.databanks.pop(name, None)

        logger.debug("[rag] discovery complete — %d databank(s) active", len(self.databanks))

    async def _do_rag_search(
        self, name: str, query: str, top_k: int, bm25_weight: float, rrf_k: int,
    ) -> list[dict]:
        """Sync the indexer then dispatch to bank.rag_search. Returns [] on any error."""
        bank    = self.databanks.get(name)
        store   = self.stores.get(name)
        indexer = self.indexers.get(name)
        if bank is None or store is None or indexer is None:
            return []
        try:
            await indexer.sync()
        except Exception as exc:
            logger.warning("[rag] sync failed for '%s': %s", name, exc)
            return []
        return await bank.rag_search(query, store, self.embedder, top_k, bm25_weight, rrf_k)

    # ------------------------------------------------------------------
    # PRE_ASSEMBLE_ASYNC — search auto-rag databanks, cache results on
    # ctx.state for the paired @prompt to read
    # ------------------------------------------------------------------

    @hook(HookType.PRE_ASSEMBLE_ASYNC)
    async def prefetch_auto_rag(self, ctx) -> None:
        ctx.state["rag_auto_results"] = {}

        # Only run on user turns
        if ctx.dialogue and ctx.dialogue[-1].role in ("tool", "assistant"):
            return

        # Read auto-rag targets from session state. `None` means this branch
        # has never touched auto-rag targets at all, so fall back to the
        # configured default; an explicit [] (from set_auto_rag_databanks([]))
        # means auto-rag was deliberately cleared and must stay off.
        raw_targets = ctx.db.get_state(ctx.tail_node_id, "rag_auto_targets")
        default_auto_targets = list(self.config["default_auto_targets"])
        targets: list[str] = raw_targets if raw_targets is not None else default_auto_targets
        if not targets:
            return

        self._sync_discovery()

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

        top_k       = int(self.config["top_k"])
        bm25_weight = float(self.config["bm25_weight"])
        rrf_k       = int(self.config["rrf_k"])
        auto_inject_semantic = bool(self.config["auto_inject_semantic"])

        auto_results_by_bank: dict[str, list[dict]] = {}
        for name in targets:
            if name not in self.databanks:
                logger.debug("[rag] auto-inject: unknown databank '%s'", name)
                continue

            bank = self.databanks[name]
            try:
                if auto_inject_semantic:
                    indexer = self.indexers.get(name)
                    if indexer is not None:
                        await indexer.sync()  # keep the semantic side of auto_inject fresh
                    store = self.stores.get(name)
                    results = await bank.auto_inject(query, store, self.embedder, top_k, bm25_weight, rrf_k)
                else:
                    results = await bank.auto_inject(query)  # keyword/regex-only, no embed() call
            except Exception as exc:
                logger.warning("[rag] auto_inject failed for '%s': %s", name, exc)
                results = []

            if results:
                auto_results_by_bank[name] = results
                logger.debug("[rag] auto-inject '%s': %d result(s)", name, len(results))

        ctx.state["rag_auto_results"] = auto_results_by_bank

    @prompt(role="system", priority=_AUTO_INJECT_PRIORITY, name="rag_auto_inject")
    def auto_rag_prompt(self, ctx) -> str:
        auto_results_by_bank = ctx.state.get("rag_auto_results", {})
        if not auto_results_by_bank:
            return ""
        budget_tokens = int(self.config["result_budget_tokens"])
        parts = []
        for name, results in auto_results_by_bank.items():
            block = _format_results(results, budget_tokens, databank_name=name)
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Tool: rag_search — process-lifetime state only, plain @tool
    # ------------------------------------------------------------------

    @tool(always_on=True, permissions=None)
    async def rag_search(self, query: str, targets: list, max_results: int = 0) -> str:
        """
        Search one or more databanks for information relevant to a query.

        Databank names come from the workspace/rag/ directory:
          - A subfolder named "lore" -> target name "lore"
          - A lorebook file named "Astraea.json" -> target name "Astraea"
        Use rag_list_databanks() first if you are unsure of the available names.
        Pass [""] (a single empty string) to search every available databank at once.

        Args:
            query:       The topic, question, or keywords to search for.
            targets:     List of databank name strings to search.
                         Example: ["Astraea"] or ["lore", "characters"].
                         Pass [""] to search all available databanks.
                         Do NOT pass a generic word like "rag" — use the actual databank name.
            max_results: Maximum results to return per databank (0 = use module default).
        """
        if not isinstance(targets, list) or not targets:
            return "Error: targets must be a non-empty list of databank names"

        top_k       = int(self.config["top_k"])
        bm25_weight = float(self.config["bm25_weight"])
        rrf_k       = int(self.config["rrf_k"])
        budget_tokens = int(self.config["result_budget_tokens"])

        k = int(max_results) if max_results and int(max_results) > 0 else top_k
        self._sync_discovery()

        if "" in targets:
            targets = sorted(self.stores.keys())
            if not targets:
                return "No databanks found — add folders or worldinfo JSON files to workspace/rag/"

        unknown = [t for t in targets if t not in self.stores]
        if unknown:
            available = sorted(self.stores.keys()) or ["(none)"]
            return (
                f"Error: unknown databank(s) {unknown}. "
                f"Available: {available}"
            )

        all_parts: list[str] = []
        for name in targets:
            results = await self._do_rag_search(name, query, k, bm25_weight, rrf_k)
            block   = _format_results(results, budget_tokens, databank_name=name)
            if block:
                all_parts.append(block)

        if not all_parts:
            return "No results found in the specified databank(s)"
        return "\n\n".join(all_parts)

    # ------------------------------------------------------------------
    # Tool: rag_list_databanks — process-lifetime state only, plain @tool
    # ------------------------------------------------------------------

    @tool(always_on=False, permissions=None)
    def rag_list_databanks(self) -> str:
        """
        List all available databanks and their types.

        Args: (none)
        """
        if not self.databanks:
            return "No databanks found — add folders or worldinfo JSON files to workspace/rag/"
        lines = ["Available databanks:"]
        for name, bank in sorted(self.databanks.items()):
            lines.append(f"  {name}  ({bank.kind})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool: set_auto_rag_databanks — needs live cycle.context/.db, TURN_START
    # ------------------------------------------------------------------

    @hook(HookType.TURN_START)
    def wire_set_auto_rag_databanks(self, cycle) -> None:
        def set_auto_rag_databanks(targets: list) -> str:
            """
            Set which databanks are automatically searched and injected into context each turn.
            Call with an empty list to disable auto-injection entirely — this overrides
            any `default_auto_targets` configured for the module (which otherwise applies
            on branches where this tool has never been called).

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

            resolved = [str(t) for t in targets]
            unknown = [t for t in resolved if t and t not in self.stores]
            if unknown:
                available = sorted(self.stores.keys()) or ["(none)"]
                return (
                    f"Error: unknown databank(s) {unknown}. "
                    f"Available: {available}"
                )

            tail = cycle.context.tail_node_id
            cycle.db.set_state(tail, "rag_auto_targets", resolved)

            if not resolved:
                return "Auto-rag cleared — no databanks will be injected automatically"
            return f"Auto-rag set to: {resolved}"

        cycle.tool_handler.register_tool(
            set_auto_rag_databanks, always_on=False, required_permissions={Permission.MANAGE_CTX},
        )

EXTENSION_META = {
    "name":    "rag",
    "version": "3.0",
    "description": (
        "Databank retrieval system. Indexes named databank folders under workspace/rag/. "
        "Markdown files may carry a YAML frontmatter header declaring keyword-triggered "
        "lore entries (native format, see lorefile.py); legacy SillyTavern lorebook JSON "
        "is auto-converted into this format on discovery and the original renamed to .bak. "
        "Provides rag_search(query, targets, max_results) and "
        "set_auto_rag_databanks(targets) tools. Auto-rag databanks are injected "
        "into the system prompt every turn via both deterministic keyword-triggered "
        "lore matching and hybrid BM25+vector search over the databank's index."
    ),
    "default_config": {
        # --- Databank root ---
        # Directory under workspace that contains named databank subdirectories.
        "rag_dir": "rag",
        # SQLite cache DBs are stored here, one per databank.
        "cache_dir": "rag/.cache",

        # --- File extensions indexed from folder databanks ---
        "indexed_extensions": [".md", ".txt", ".rst"],

        # --- Chunking ---
        # Strategy name: "markdown" | "tokens" | "chars" | "delimiter"
        "chunk_strategy": "markdown",
        # Strategy kwargs — passed through to get_strategy(); leave empty for defaults.
        "chunk_kwargs": {},

        # --- Embedding ---
        # Key from models: with kind: embedding, or "" for BM25-only mode.
        "embedding_model": "",

        # --- Retrieval ---
        # Default max chunks returned by rag_search when max_results is not specified.
        "top_k": 5,
        # BM25 share of hybrid score (vector weight = 1 - bm25_weight),
        # fused via reciprocal-rank fusion.
        "bm25_weight": 0.3,
        # RRF's rank-damping constant — higher values flatten the fusion
        # curve (top ranks matter less relative to lower ones).
        "rrf_k": 60,

        # --- Result budget ---
        # Maximum tokens the formatted result block may occupy.
        # Set to 0 to disable budget enforcement.
        "result_budget_tokens": 2048,

        # --- Auto-inject ---
        # System prompt priority for the auto-rag injected block.
        "auto_inject_priority": 25,
        # Databank names auto-searched/injected every turn on branches that have
        # never called set_auto_rag_databanks. Once that tool is called on a
        # branch (even with []), its stored value wins over this default.
        "default_auto_targets": [],
        # Whether auto-inject's passive half also runs the hybrid BM25+vector
        # search (in addition to deterministic keyword/regex/constant firing).
        # This costs a real embed() network call on every qualifying turn,
        # sharing ai.py's process-wide priority queue with the main LLM call
        # and everything else — on a low `parallel:` deployment this can add
        # meaningful per-turn latency under concurrent load. Set to false to
        # fall back to keyword/regex-only auto-inject (no embed() call at all);
        # rag_search (the explicit tool) always does the full hybrid search
        # regardless of this setting.
        "auto_inject_semantic": True,
    },
}

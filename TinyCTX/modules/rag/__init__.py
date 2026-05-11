EXTENSION_META = {
    "name":    "rag",
    "version": "3.0",
    "description": (
        "Databank retrieval system. Indexes named databank folders under workspace/rag/. "
        "Provides rag_search(query, targets, max_results) and "
        "set_auto_rag_databanks(targets) tools. Auto-rag databanks are injected "
        "into the system prompt every turn via hybrid BM25+vector search."
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
        # BM25 share of hybrid score (vector weight = 1 - bm25_weight).
        "bm25_weight": 0.3,

        # --- Result budget ---
        # Maximum tokens the formatted result block may occupy.
        # Set to 0 to disable budget enforcement.
        "result_budget_tokens": 2048,

        # --- Auto-inject ---
        # System prompt priority for the auto-rag injected block.
        "auto_inject_priority": 25,
    },
}

EXTENSION_META = {
    "name":    "memory",
    "version": "2.0",
    "description": (
        "Long-term memory backed by a scoped LadybugDB property graph "
        "(memory.lbug). Extractor librarians ingest conversation into the graph; "
        "Reviewer librarians run flagger-driven maintenance; a Deduper merges "
        "semantic duplicates. The main agent reads via search_memory / "
        "memory_stats and triggers maintenance via call_librarian. Passive RAG "
        "(BM25 + vector, min-p before RRF) and pinned entities are injected into "
        "the system prompt as a <memory> block, restricted to the active scope."
    ),
    "default_config": {
        # --- paths (relative to the internal data dir) ---
        "graph_path":    "memory/memory.lbug",
        "librarian_log": "memory/librarian.log",

        # --- embedding (single model; "" = BM25-only) ---
        "embedding_model": "",

        # --- read-time weighting shared across flaggers ---
        "mention_half_life_days": 30,

        # --- passive RAG + memory block ---
        "passive_rag": {
            "enabled":             True,
            "memory_block_tokens": 2048,
            "min_p":               0.30,   # applied BEFORE RRF
            "search_min_p":        0.0,    # vector floor for search_memory
            "bm25_weight":         0.40,
            "rrf_k":               60,
            "mention_bump":        0.1,
        },

        # --- pinned entities ---
        "pins": {
            "include_neighbors": False,
            "priority":          5,
            "user_scan":         3,
            "max_per_scope":     12,
        },

        # --- librarian runner ---
        "librarian": {
            "trigger_interval_hours":     6,
            "batch_size":                 20,
            "max_concurrent":             4,
            "model":                      "",
            "ingest_pressure_ratio":      0.5,
            "ingest_pressure_min_tokens": 500,
        },

        # --- reviewer ---
        "reviewer": {
            "enabled":        True,
            "interval_hours": 6,
            "base_delay":     30,
            "min_delay":      2,
            "target_len":     10,
        },

        # --- reviewer flaggers ---
        "flaggers": {
            "max_edges_between":           4,
            "desc_max_chars":              1200,
            "desc_min_chars":              15,
            "fuzzy_name_threshold":        95,
            "decay_min_effective_mention": 0.5,
            "decay_max_edges":             1,
            "decay_stale_days":            90,
        },

        # --- deduper ---
        "dedup": {
            "enabled":              True,
            "interval_hours":       6,
            "similarity_threshold": 0.85,
            "batch_count":          8,
        },
    },
}

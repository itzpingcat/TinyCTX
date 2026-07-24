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
        "embedding_model":        "",
        "embed_query_template":   "{text}",
        "embed_document_template": "{text}",

        # --- passive RAG + memory block ---
        "passive_rag_enabled":   True,
        "memory_block_tokens":   2048,
        "passive_min_p":         0.30,   # applied BEFORE RRF
        "search_min_p":          0.0,    # vector floor for search_memory
        "bm25_weight":           0.40,
        "rrf_k":                 60,
        "passive_mention_bump":  0.1,
        "pin_include_neighbors": False,
        "pinned_priority":       5,
        "pinned_user_scan":      3,
        "mention_half_life_days": 30,    # read-time weighting for flaggers only

        # --- librarian runner ---
        "trigger_interval_hours":     6,
        "batch_size":                 20,
        "max_concurrent":             4,
        "ingest_pressure_ratio":      0.5,
        "ingest_pressure_min_tokens": 500,
        "librarian_model":            "",

        # --- reviewer / flaggers ---
        "reviewer_enabled":        True,
        "reviewer_interval_hours": 6,
        "reviewer_base_delay":     30,
        "reviewer_min_delay":      2,
        "reviewer_target_len":     10,
        "max_edges_between":       4,
        "desc_max_chars":          1200,
        "desc_min_chars":          15,
        "max_pins_per_scope":      12,
        "decay_min_effective_mention": 0.5,
        "decay_max_edges":         1,
        "decay_stale_days":        90,
        "fuzzy_name_threshold":    95,

        # --- deduper ---
        "dedup_enabled":        True,
        "dedup_interval_hours": 6,
        "similarity_threshold": 0.90,
        "dedup_batch_count":    8,
    },
}

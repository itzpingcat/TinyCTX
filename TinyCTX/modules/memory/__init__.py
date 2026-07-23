EXTENSION_META = {
    "name":    "memory",
    "version": "0.2",
    "description": (
        "Long-term memory backed by a LadybugDB property graph. "
        "An in-process librarian runner watches agent.db for unvisited "
        "conversation nodes, extracts entities and relationships, and writes them "
        "to the graph. The main agent reads via kg_search / kg_traverse tools "
        "and can trigger librarian agents on demand via call_librarian. "
        "Pinned entities are injected into the system prompt automatically."
    ),
    "default_config": {
        # Paths (relative to workspace)
        "graph_path":    "memory/graph.lbug",
        "librarian_log": "memory/librarian.log",

        # Librarian trigger config
        "trigger_interval_hours": 6,
        "batch_size":             20,
        "max_concurrent":         4,

        # Dedup cycle
        "dedup_enabled":          True,
        "dedup_interval_hours":   6,
        "similarity_threshold":   0.90,

        # Decay sweep — hard-deletes non-pinned entities scoring below
        # decay_threshold. Score blends priority, distance to nearest pinned
        # entity (BFS capped at decay_max_hops), active edge count, mention
        # count, and read/update recency (half-life in days). Pinned entities
        # are never scored or touched. Weights need not sum to 1.
        "decay_enabled":            True,
        "decay_interval_hours":     6,
        "decay_threshold":          0.25,
        "decay_max_hops":           4,
        "decay_half_life_days":     30,
        "decay_weight_priority":    0.30,
        "decay_weight_distance":    0.20,
        "decay_weight_edges":       0.15,
        "decay_weight_mentions":    0.15,
        "decay_weight_recency":     0.20,

        # Embedding model key from config.yaml models: (must be kind: embedding)
        # Leave empty to disable semantic search (keyword only)
        "embedding_model": "",

        # Templates applied to text before embedding. Use {text} as the placeholder.
        # e.g. "Query: {text}" / "Document: {text}" for models like Jina.
        "embed_query_template":    "{text}",
        "embed_document_template": "{text}",

        # Separate embedding model for dedup/graph similarity.
        # When set, graph_embedding is stored alongside the regular embedding.
        # Dedup uses graph_embedding, falling back to embedding when absent.
        # Leave empty to reuse embedding_model for dedup as well.
        "graph_embedding_model": "",

        # Pinned entity injection priority in system prompt
        "pinned_priority": 5,

        # How many user messages to scan back to find active participants.
        # A pinned_target=<username> entity is only injected when that user
        # appears within this many recent user turns.
        "pinned_user_scan": 3,

        # Token budget for the <memory> block injected into system prompt
        "memory_block_tokens": 2048,

        # LLM model key for librarian agents (defaults to primary)
        "librarian_model": "",

        # Pressure-based ingest: trigger a branch buffer-agent ingest after
        # this fraction of the context window worth of new tokens has been
        # written on a branch. 0.5 = half a context window. 0 = disabled.
        "ingest_pressure_ratio": 0.5,

        # Minimum new tokens required before pressure ingest can fire.
        # Guards against triggering on very short threads.
        "ingest_pressure_min_tokens": 500,
    },
}

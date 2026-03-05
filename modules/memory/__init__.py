EXTENSION_META = {
    "name":    "memory",
    "version": "1.0",
    "description": (
        "Injects workspace markdown files (SOUL.md, AGENTS.md, MEMORY.md) "
        "as system prompt providers. Files are re-read on every assemble() "
        "so edits take effect without restart. Missing files are silently skipped."
    ),
    "default_config": {
        "soul_file":    "SOUL.md",
        "agents_file":  "AGENTS.md",
        "memory_file":  "MEMORY.md",
        # Priority in the system prompt — lower = injected first
        "soul_priority":   0,
        "agents_priority": 10,
        "memory_priority": 20,
    },
}
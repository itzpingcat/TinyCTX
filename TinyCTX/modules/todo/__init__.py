EXTENSION_META = {
    "name":    "todo",
    "version": "1.0",
    "description": (
        "Persistent per-session task checklist. The agent uses todo_write to "
        "track multi-step work across tool cycles. The current list is injected "
        "into context every turn so the agent never loses track."
    ),
    "default_config": {
        "prompt_priority": 8,
    },
}

EXTENSION_META = {
    "name": "subagents",
    "version": "0.1",
    "description": (
        "Detached child-agent tools for bounded parallel work. "
        "Provides spawn_agent and wait_agent on top of TinyCTX branch execution."
    ),
    "default_config": {
        "prompt_priority": 13,
        "max_concurrent": 4,
        "completed_ttl_seconds": 900.0,
    },
}

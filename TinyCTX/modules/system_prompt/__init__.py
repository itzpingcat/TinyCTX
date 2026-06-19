"""
modules/system_prompt

Injects workspace markdown files (SOUL.md, AGENTS.md, TOOLS.md)
as system prompt providers. RAG config and EXTENSION_META live in modules/rag.
"""

EXTENSION_META = {
    "name":    "system_prompt",
    "version": "1.0",
    "description": (
        "Injects workspace markdown files (SOUL.md, AGENTS.md, TOOLS.md) "
        "as system prompt providers."
    ),
    "default_config": {
        "soul_file":    "SOUL.md",
        "agents_file":  "AGENTS.md",
        "tools_file":   "TOOLS.md",
        "soul_priority":   0,
        "agents_priority": 10,
        "tools_priority":  15,
    },
}

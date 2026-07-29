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
        "soul":   {"file": "SOUL.md",   "priority": 0},
        "agents": {"file": "AGENTS.md", "priority": 10},
        "tools":  {"file": "TOOLS.md",  "priority": 15},
    },
}

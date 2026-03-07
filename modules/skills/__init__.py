EXTENSION_META = {
    "name":    "skills",
    "version": "1.0",
    "description": (
        "Agent Skills support (agentskills.io). "
        "Discovers SKILL.md files from configured directories, injects a compact "
        "skill index into the system prompt, and exposes use_skill / list_skills / "
        "read_skill_file tools so the LLM can load full instructions on demand. "
        "Skills are hot-reloaded — drop a new folder in and it appears immediately."
    ),
    "default_config": {
        # Directories to scan for skills, in priority order.
        # Relative paths are resolved against the workspace root.
        # The cross-client convention paths are always scanned too:
        #   ~/.tinyctx/skills/  and  .tinyctx/skills/  (cwd)
        "skill_dirs": [
            "skills",           # ~/.tinyctx/skills/  (primary user location)
        ],
        # Approximate max tokens per skill entry in the index prompt.
        # name + description should comfortably fit in ~60 tokens.
        "index_priority": 5,    # system prompt priority (after soul, before memory)
    },
}
EXTENSION_META = {
    "name":        "cron",
    "version":     "2.0",
    "module_type": "singleton",   # runs once on the system lane; not per-lane
    "description": (
        "Scheduled agent turns. Jobs are stored in a SQLite database under the "
        "instance's internal data dir (config.data.path/cron.db) — never in "
        "workspace/, so the agent's own filesystem tools cannot create or edit "
        "jobs directly (closes a stored prompt-injection path). Supports three "
        "schedule kinds: 'at' (one-shot timestamp), 'every' (fixed interval), and "
        "'cron' (cron expression via croniter). Jobs are created, listed, and "
        "removed only via the add_cron / list_cron / remove_cron tools: "
        "  - add_cron runs as the calling user's real identity and channel "
        "    (cursor_key); the job later fires with that same user's *current* "
        "    permission_level, re-resolved at run time, not a fixed system level. "
        "  - list_cron / remove_cron only see/act on jobs created in the caller's "
        "    own channel; remove_cron additionally requires being the job's "
        "    creator, or a caller permission_level >= the configurable admin "
        "    override threshold. "
        "Job output is delivered back to the originating channel through "
        "Runtime.deliver() (the same per-platform renderer a live turn uses), "
        "instead of being silently discarded as in v1."
    ),
    "default_config": {
        # Path relative to config.data.path (NOT workspace — see description).
        "store_file": "cron.db",
        # Minimum permission_level a job's *stored creator* must hold at run
        # time for the job to execute — re-checked every run, not just at
        # creation, so a later demotion disables the job's future runs.
        "min_run_permission": 0,
        # Minimum permission_level to register a new job at all.
        "min_create_permission": 25,
        # Caller permission_level at or above this may remove any job in
        # their own channel, not just ones they created.
        "admin_override_permission": 90,
    },
}
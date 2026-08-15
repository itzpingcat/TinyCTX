EXTENSION_META = {
    "name":        "cron",
    "version":     "3.0",
    "module_type": "singleton",   # runs once on the system lane; not per-lane
    "description": (
        "Scheduled agent turns. Jobs are stored in a SQLite database under the "
        "instance's internal data dir (config.data.path/cron.db) — never in "
        "workspace/, so the agent's own filesystem tools cannot create or edit "
        "jobs directly (closes a stored prompt-injection path). Scheduling is a "
        "single cron expression (via croniter) plus a one_shot flag for single "
        "reminders — a one_shot job is a cron expression matching one specific "
        "future minute, computed by the agent from the current time, rather than "
        "a separate schedule shape. Jobs are created, listed, and removed only "
        "via the add_cron / list_cron / remove_cron tools, whose docstrings are "
        "written for the agent's own use (schedule reminders/recurring checks; "
        "the job's message is what the agent itself receives when it fires, not "
        "what's shown to the person who asked) rather than as raw API reference: "
        "  - add_cron runs as the calling user's real identity and channel "
        "    (cursor_key), and requires the caller hold CRON_CREATE. The job "
        "    later fires only if its *stored creator* currently holds "
        "    CRON_CREATE — re-checked via effective_permissions() every run, "
        "    not just at creation, so a later revocation disables the job's "
        "    future runs (skipped wholesale, not partially executed — see "
        "    docs/PERMISSIONS-PLAN.md §8). "
        "  - list_cron / remove_cron only see/act on jobs created in the caller's "
        "    own channel; remove_cron additionally requires being the job's "
        "    creator, or the caller holding CRON_ADMIN. "
        "Job output is delivered back to the originating channel through "
        "Runtime.deliver() (the same per-platform renderer a live turn uses), "
        "instead of being silently discarded as in v1."
    ),
    "default_config": {
        # Path relative to config.data.path (NOT workspace — see description).
        "store_file": "cron.db",
        # NOTE: min_run_permission, min_create_permission, and
        # admin_override_permission are GONE — all three collapsed into a
        # single named bool per docs/PERMISSIONS-PLAN.md §8:
        #   min_run_permission / min_create_permission -> CRON_CREATE
        #     (checked on the caller at create time, and on the stored
        #     creator at run time)
        #   admin_override_permission -> CRON_ADMIN
        #     (checked on the caller, to act on someone else's job)
    },
}
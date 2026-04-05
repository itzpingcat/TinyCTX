EXTENSION_META = {
    "name":    "cron",
    "version": "1.0",
    "description": (
        "Scheduled agent turns. Jobs are stored in workspace/CRON.json "
        "and support three schedule kinds: 'at' (one-shot timestamp), "
        "'every' (fixed interval), and 'cron' (cron expression via croniter). "
        "Exposes a single cron_list tool for the agent to inspect and validate jobs. "
        "Jobs are added/edited/removed directly via str_replace on CRON.json."
    ),
    "default_config": {
        # Path relative to workspace
        "store_file": "CRON.json",
    },
}
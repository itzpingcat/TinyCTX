EXTENSION_META = {
    "name":    "shell",
    "version": "1.2",
    "description": (
        "Shell execution tool. "
        "shell: always-on, runs in the sandbox container by default (no LAN/Tailscale). "
        "Pass backend_access=True to run in the main TinyCTX container with full network access "
        "and its own backend files (permission level 80 required). "
        "Blacklist enforced before dispatch in both modes."
    ),
    "default_config": {
        # Timeout used when the agent does not pass an explicit timeout arg.
        "default_timeout": 120,

        # Hard ceiling — agent-supplied timeout values are capped to this.
        "max_timeout": 1200,

        # Default points at the sandbox container defined in compose.yaml.
        # Actual host is computed at runtime from TINYCTX_INSTANCE (the
        # per-instance hashed container name) + "_sandbox" — see
        # modules/shell/__main__.py::register_agent. Override to null for
        # bare-metal / Windows / dev (falls back to local).
        "sandbox_url": None,
    },
}

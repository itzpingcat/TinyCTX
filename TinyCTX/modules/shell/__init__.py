EXTENSION_META = {
    "name":    "shell",
    "version": "1.2",
    "description": (
        "Shell execution tool. "
        "shell: always-on, runs in the sandbox container by default (no LAN/Tailscale). "
        "Pass internal_network=True to run in the main TinyCTX container with full network access "
        "(permission level 80 required). "
        "Blacklist enforced before dispatch in both modes."
    ),
    "default_config": {
        # Timeout used when the agent does not pass an explicit timeout arg.
        "default_timeout": 120,

        # Hard ceiling — agent-supplied timeout values are capped to this.
        "max_timeout": 1200,

        # Default points at the sandbox container defined in compose.yaml.
        # Override to null for bare-metal / Windows / dev (falls back to local).
        "sandbox_url": "http://tinyctx_sandbox:8700",
    },
}

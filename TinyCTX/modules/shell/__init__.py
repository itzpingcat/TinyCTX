EXTENSION_META = {
    "name":    "shell",
    "version": "1.1",
    "description": (
        "Shell execution tools. "
        "shell: always-on, runs in the sandbox container (no LAN/Tailscale). "
        "core_shell: deferred, runs directly on the host — permission 100 only. "
        "Blacklist enforced on both before dispatch."
    ),
    "default_config": {
        "timeout": 60,

        # Default points at the sandbox container defined in compose.yaml.
        # Override to null for bare-metal / Windows / dev (falls back to local).
        "sandbox_url": "http://tinyctx_sandbox:8700",
    },
}

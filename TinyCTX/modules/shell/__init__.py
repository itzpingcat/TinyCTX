EXTENSION_META = {
    "name":    "shell",
    "version": "1.3",
    "description": (
        "Shell execution tool. "
        "shell: always-on, runs in the sandbox container by default (no LAN/Tailscale). "
        "Pass backend_access=True to run in the main TinyCTX container with full network access "
        "and its own backend files (requires permissions.access_backend). "
        "Callers below permissions.neutral may only run commands listed in whitelist.txt. "
        "Blacklist enforced before dispatch in both modes unless caller is at permissions.bypass_blacklist."
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

        # Permission levels (0-100) gating shell access. Resolved from the
        # actual caller (agent.caller.permission_level) at call time, never
        # from a static config value. Override per-instance via:
        #   extra:
        #     shell:
        #       permissions:
        #         use_whitelist: 10
        #         neutral: 45
        #         bypass_blacklist: 90
        #         access_backend: 80
        "permissions": {
            # Min level to call the shell tool at all. Below "neutral",
            # every command must match an entry in whitelist.txt.
            "use_whitelist": 25,

            # Min level for unrestricted commands (still blacklist-checked
            # unless bypass_blacklist).
            "neutral": 45,

            # Min level that skips the blacklist check entirely.
            "bypass_blacklist": 90,

            # Min level required for backend_access=True.
            "access_backend": 80,
        },
    },
}

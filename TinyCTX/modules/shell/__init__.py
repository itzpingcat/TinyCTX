EXTENSION_META = {
    "name":    "shell",
    "version": "2.0",
    "description": (
        "Shell execution tool. "
        "shell: always-on, runs in the sandbox container by default (no LAN/Tailscale). "
        "Pass backend_access=True to run in the main TinyCTX container with full network access "
        "and its own backend files (requires permissions.access_backend). "
        "Commands are parsed with tree-sitter-bash and each resolved command is checked against "
        "a YAML policy: deny.yaml (allow-by-default) for callers at permissions.neutral and above, "
        "allow.yaml (deny-by-default, with per-command subcommand/flag constraints) for callers "
        "below it. Callers at permissions.bypass_blacklist skip policy checks entirely."
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
        # bare-metal / dev (falls back to local). Linux only.
        "sandbox_url": None,

        # Command policy files. null uses the defaults shipped in the module
        # (modules/shell/deny.yaml and allow.yaml). Override to point at your
        # own — typically in the instance directory, mounted READ-ONLY into
        # the container:
        #   extra:
        #     shell:
        #       policy:
        #         deny:  /instance/shell-deny.yaml
        #         allow: /instance/shell-allow.yaml
        #
        # Files are loaded once and cached; editing one requires a restart.
        # A missing or malformed policy file blocks EVERY command — the
        # shell never degrades to unrestricted when its rules fail to load.
        "policy": {
            "deny": None,
            "allow": None,
        },

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
            # every command must be permitted by allow.yaml.
            "use_whitelist": 25,

            # Min level for unrestricted commands (still checked against
            # deny.yaml unless bypass_blacklist).
            "neutral": 45,

            # Min level that skips policy checks entirely.
            "bypass_blacklist": 90,

            # Min level required for backend_access=True.
            "access_backend": 80,
        },
    },
}

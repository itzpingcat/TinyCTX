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

        # Level below which the shell tool isn't offered at all. This is the
        # tool's min_permission in the tool handler.
        "min_permission": 30,

        # Command policies, each with the level at which a caller outgrows it.
        # A caller is subject to EVERY policy whose applies_below they are
        # under. With the defaults here:
        #
        #   level 30-44   allow.yaml AND deny.yaml   (allow-listed commands only)
        #   level 45-89   deny.yaml                  (anything not denied)
        #   level 90+     nothing                    (unrestricted)
        #
        # Omit applies_below for a policy that binds everyone, with no
        # unrestricted tier at all.
        #
        # `policy:` accepts:
        #   builtin:allow      modules/shell/allow.yaml (deny-by-default)
        #   builtin:deny       modules/shell/deny.yaml  (allow-by-default)
        #   name.yaml          relative to <instance>/config/, which compose
        #                      binds read-only at /app/config — use this form,
        #                      it resolves correctly both on the host and in
        #                      the container
        #   /abs/path.yaml     literal; you keep it mounted
        #
        # Add or remove entries freely — the module has no opinion on how many
        # tiers exist, and doesn't care which file is an allow-list and which
        # is a deny-list (each declares that itself via default_action).
        #
        # To layer onto a shipped policy rather than replace it, drop a small
        # file with `extends: builtin:allow` into <instance>/config/ and name
        # it here — see modules/shell/example.instance-allow.yaml.
        #
        # Policy files are loaded once and cached; editing one requires a
        # restart. A missing or malformed policy blocks EVERY command — the
        # shell never degrades to unrestricted when its rules fail to load.
        "policies": [
            {"policy": "builtin:allow", "applies_below": 45},
            {"policy": "builtin:deny", "applies_below": 90},
        ],

        # Resolved from the actual caller (agent.caller.permission_level) at
        # call time, never from a static config value.
        #
        # NOTE: use_whitelist / neutral / bypass_blacklist used to live here.
        # They are gone — min_permission plus the policies list above express
        # all three, and more. Leaving them in a config is an ERROR that
        # blocks the shell rather than being ignored, since ignoring one would
        # silently loosen access for anyone who had set it to lock things down.
        "permissions": {
            # Min level required for backend_access=True. Gates WHERE a
            # command runs (main container vs sandbox), which is orthogonal
            # to which policy applies to it — hence still a scalar.
            "access_backend": 80,
        },
    },
}

EXTENSION_META = {
    "name":    "shell",
    "version": "3.0",
    "description": (
        "Shell execution tool. "
        "shell: always-on, runs in the sandbox container by default (no LAN/Tailscale). "
        "Pass backend_access=True to run in the main TinyCTX container with full network access "
        "and its own backend files (requires the backend_exec capability). "
        "Commands are parsed with tree-sitter-bash. Each resolved command is classified "
        "into the capability bools it needs (file_read, file_write, network_read, "
        "network_write, untrusted_exec, ...) and checked once, centrally, by "
        "tool_handling.handler.ToolCallHandler — see modules/shell/perms.py and "
        "docs/PERMISSIONS-PLAN.md §5. A single always-applied shape policy (derived from "
        "allow.yaml's `constructs` map) still runs underneath that, rejecting `$()`, "
        "unquoted globs used as commands, and unrecognized bash syntax — structural "
        "injection defense, orthogonal to capability checking (§5.2)."
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

        # NOTE: min_permission, policies (applies_below tiers), and
        # permissions.access_backend are GONE — permission_level was fully
        # retired (see TinyCTX/permissions.py and docs/PERMISSIONS-PLAN.md).
        # Which commands a caller may run is now decided entirely by their
        # granted capabilities (the single permissions.template in
        # config.yaml, plus any per-user permission_overrides), via
        # modules/shell/perms.py's per-command classification. There is
        # nothing left to configure here for that axis.
    },
}

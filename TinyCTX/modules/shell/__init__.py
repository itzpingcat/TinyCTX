EXTENSION_META = {
    "name":    "shell",
    "version": "1.0",
    "description": (
        "Shell execution tool. Runs bash commands in the workspace. "
        "Enforces a command blacklist before dispatch. "
        "When sandbox_url is configured, commands run inside an isolated "
        "sandbox container (no LAN/Tailscale access). "
        "Falls back to local bash/PowerShell when sandbox_url is unset."
    ),
    "default_config": {
        "timeout": 60,

        # Set sandbox_url to the sandbox service base URL to enable sandboxed execution.
        # In Docker Compose this is: http://tinyctx_sandbox:8700
        # Leave null to run commands locally (bare-metal / Windows / dev).
        "sandbox_url": None,
    },
}

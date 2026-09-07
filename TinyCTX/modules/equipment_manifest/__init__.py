EXTENSION_META = {
    "name":    "equipment_manifest",
    "version": "1.0",
    "description": (
        "Injects a rendered Equipment Manifest (EM.md) as a system prompt. "
        "EM.md is a Jinja2-lite template that may use {% if %}/{% else %}/{% endif %} "
        "blocks and {{ variable }} substitutions. Available variables: "
        "system (OS name), date, time, workspace_path, config_path, "
        "is_group_chat, is_dm, platform, trusted, "
        "server_name (guild/server name, empty string for DMs/CLI), "
        "channel_name (channel or thread name, empty string for DMs/CLI). "
        "If EM.md is missing or empty, the module is a no-op."
    ),
    "default_config": {
        # Path to EM.md.
        # - Empty string: the EM.md next to this __init__.py
        # - Relative path: resolved against the workspace root
        # - Absolute path: used as-is
        # - "workspace:EM.md": workspace-relative (same as a plain relative path)
        "em_path": "",
        # Set to false to disable this module without removing it from config.
        "enabled": True,
        # System prompt priority (lower = earlier in the prompt).
        "prompt_priority": 5,
        # NOTE: trusted_threshold is GONE. The `trusted` template variable is
        # now True when the requesting user holds Permission.EQUIPMENT_TRUSTED
        # (true in both DMs and group chats — see _build_variables below;
        # this comment previously said "DM only", which was stale).
        # docs/PERMISSIONS-PLAN.md §10.3: this bool is a *disclosure* flag —
        # it decides whether the system prompt tells the agent about
        # sensitive equipment, not whether an action is authorised. It's a
        # category of one in TinyCTX.permissions.Permission; don't assume
        # every Permission member gates an action just because most do.
    },
}

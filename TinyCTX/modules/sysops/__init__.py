EXTENSION_META = {
    "name":        "sysops",
    "version":     "2.0",
    "module_type": "per-cycle",
    "description": (
        "User and permission management tools for the agent, plus the /model "
        "command and its set_active_model tool for switching the LLM used on "
        "a conversation branch. Exposes user_list, user_info, "
        "user_modify_permissions, user_rename, user_merge, and "
        "set_active_model as agent-callable tools (always_on=False). "
        "Gating is via named capabilities (TinyCTX.permissions.Permission), "
        "enforced centrally by ToolCallHandler / CommandRegistry — see "
        "docs/PERMISSIONS-PLAN.md. user_modify_permissions grants or revokes "
        "a single permission bool on a user's permission_overrides — there "
        "is one global permissions.template (config.yaml) shared by every "
        "user; there is no more numeric ceiling logic since ROOT is total."
    ),
    "default_config": {
        # NOTE: model_min_permission is GONE — /model and set_active_model
        # are both gated on Permission.MODEL_SWAP (a bool granted by the
        # global template or a per-user override), not a configurable
        # numeric threshold. See docs/PERMISSIONS-PLAN.md §9's table.
    },
}

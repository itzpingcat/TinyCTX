EXTENSION_META = {
    "name":        "sysops",
    "version":     "1.0",
    "module_type": "per-cycle",
    "description": (
        "User and permission management tools for the agent, plus the /model "
        "command and its set_active_model tool for switching the LLM used on "
        "a conversation branch. Exposes user_list, user_info, user_grant, "
        "user_rename, user_merge, and set_active_model as agent-callable "
        "tools (always_on=False). User mutations respect caller permission "
        "level — you cannot grant a level higher than your own, and you "
        "cannot touch users whose current level exceeds yours."
    ),
    "default_config": {
        # Minimum permission_level required to view or change the model
        # override, via either /model or the set_active_model tool.
        # Override per-instance via config.yaml:
        #   extra:
        #     sysops:
        #       model_min_permission: 90
        "model_min_permission": 75,
    },
}

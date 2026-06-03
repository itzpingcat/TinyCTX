EXTENSION_META = {
    "name":        "sysops",
    "version":     "1.0",
    "module_type": "per-cycle",
    "description": (
        "User and permission management tools for the agent. "
        "Exposes user_list, user_info, user_grant, user_rename, and user_merge "
        "as agent-callable tools. All mutations respect caller permission level — "
        "you cannot grant a level higher than your own, and you cannot touch users "
        "whose current level exceeds yours. always_on=False."
    ),
    "default_config": {},
}

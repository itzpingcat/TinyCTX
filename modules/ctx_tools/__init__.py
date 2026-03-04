EXTENSION_META = {
    "name":    "ctx_tools",
    "version": "1.0",
    "description": "Core context optimizations: dedup and trim.",
    "default_config": {
        "same_call_dedup_after":      3,
        "tool_trim_after":            10,
        "tool_output_truncate_after": 2,
        "max_tool_output_chars":      2000,
    },
}
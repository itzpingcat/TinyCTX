EXTENSION_META = {
    "name":    "ctx_tools",
    "version": "1.1",
    "description": "Core context optimizations: dedup, CoT strip, and trim.",
    "default_config": {
        "same_call_dedup_after":      3,
        "cot_keep_recent_turns":      10000,
        "tool_trim_after":            10,
        "tool_output_truncate_after": 2,
        "max_tool_output_chars":      2000,
    },
}
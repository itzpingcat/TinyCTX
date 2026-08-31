EXTENSION_META = {
    "name":    "ctx_tools",
    "version": "1.1",
    "description": "Core context optimizations: dedup, CoT strip, and trim.",
    "default_config": {
        "same_call_dedup_after": 2,
        "cot_keep_recent_turns": 10000,
        "tokenade_threshold":    20000,
        "tool_output": {
            "trim_after":     25,
            "truncate_after": 10,
            "max_chars":      2000,
        },
    },
}

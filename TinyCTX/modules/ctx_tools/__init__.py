EXTENSION_META = {
    "name":    "ctx_tools",
    "version": "1.1",
    "description": "Core context optimizations: dedup, CoT strip, and trim.",
    "default_config": {
        "same_call_dedup_after": 2,
        # "all"  — strip every <think>...</think> block, including the one
        #          just produced this cycle.
        # "auto" — keep <think> blocks belonging to the agentcycle that's
        #          still in progress (i.e. every assistant turn newer than
        #          the most recent user turn); strip everything older. This
        #          is what a model that expects to see its own prior
        #          reasoning mid-cycle (e.g. across tool calls) needs.
        # "none" — never strip; every stored <think> block stays (subject to
        #          normal token-budget trimming like any other content).
        "trim_thinking":         "auto",
        "tokenade_threshold":    20000,
        # Max chars of a streamed reply's opening text to buffer while
        # checking for a spoofed label prefix (see __main__.py's
        # _LabelPrefixStripHook / agent.py's stream_text_hooks).
        "label_prefix_strip_max_chars": 40,
        "tool_output": {
            "trim_after":     25,
            "truncate_after": 10,
            "max_chars":      2000,
        },
    },
}

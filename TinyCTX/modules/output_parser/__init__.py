EXTENSION_META = {
    "name":    "output_parser",
    "version": "1.0",
    "description": (
        "Detects tool calls a model emitted as plain text (fenced blocks, "
        "<tool_call> tags, bare JSON, or Pythonic call syntax) instead of a "
        "native tool call, and nudges it back onto the native channel. "
        "Diagnoses — once per session — native-but-unparsed formats "
        "(e.g. LFM2/Liquid Pythonic calls) that a nudge can't fix."
    ),
    "default_config": {
        "enabled": True,
        # Max HOOK_POST_COMPLETION nudges to issue per AgentCycle before giving
        # up and falling back to a single diagnostic notify instead. Keeps a
        # model that's structurally incapable of native calling (wrong
        # --jinja template, etc.) from being nudged forever.
        "max_nudges_per_cycle": 2,
    },
}

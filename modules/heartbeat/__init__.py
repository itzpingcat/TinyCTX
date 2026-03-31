EXTENSION_META = {
    "name":    "heartbeat",
    "version": "2.0",
    "description": (
        "Periodic agent turns on a configurable interval, isolated on their own "
        "DB branch — never polluting the user's conversation thread. "
        "Branch strategy is configurable: 'root' (fully isolated) or 'session' "
        "(branches off the user's session tail at startup). "
        "Replies of HEARTBEAT_OK are silently dropped."
    ),
    "default_config": {
        # Interval in minutes. 0 = disabled.
        "every_minutes":   30,
        # Where to branch the heartbeat thread from.
        #   "root"    — child of the global DB root, fully isolated from user history
        #   "session" — child of the agent's session tail at heartbeat startup
        #               (inherits history up to that point, then diverges)
        "branch_from":     "root",
        # Prompt sent verbatim as the heartbeat user message.
        "prompt":          (
            "Read HEARTBEAT.md if it exists (workspace context). "
            "Follow it strictly. Do not infer or repeat old tasks from prior chats. "
            "If nothing needs attention, reply HEARTBEAT_OK."
        ),
        # After stripping HEARTBEAT_OK, how many chars are still allowed before
        # we treat the reply as an alert worth surfacing.
        "ack_max_chars":   300,
        # Prompt sent when the agent hasn't returned HEARTBEAT_OK yet.
        "continuation_prompt": (
            "Continue the task, or reply HEARTBEAT_OK when you are done."
        ),
        # Max number of continuation turns before giving up and moving on.
        "max_continuations":   5,
        # active_hours: restrict heartbeats to a time window. null = always run.
        # Example: {"start": "09:00", "end": "22:00"}
        "active_hours":    None,
    },
}

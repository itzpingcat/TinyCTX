EXTENSION_META = {
    "name":    "web",
    "version": "1.2",
    "description": (
        "Web tools: DuckDuckGo search, open_url (fetch or browser-render a page returning "
        "elements/text/html), async HTTP requests, and Playwright browser automation "
        "(click, type, extract, screenshot). "
        "Screenshots are saved to workspace/downloads/. "
        "One browser instance per agent session."
    ),
    "default_config": {
        "headless":              False,
        "timeout_ms":            30000,
        "wait_until":            "domcontentloaded",
        "shift_enter_for_newline": True,
        "ignore_tags":           ["script", "style"],
        "max_discovery_elements": 40,
        "browse_max_bytes":      2000000,
        "browse_max_chars":      20000,
        "browse_user_agent":     "TinyCTX/1.1",
        "prompt_priority":       12,
        "search_results":        5,
        "downloads_dir":         "downloads",
    },
}

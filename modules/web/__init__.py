EXTENSION_META = {
    "name":    "web",
    "version": "1.1",
    "description": (
        "Web tools: DuckDuckGo search, direct page browsing/scraping, async HTTP requests, "
        "and Playwright browser automation (navigate, click, type, extract, screenshot). "
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

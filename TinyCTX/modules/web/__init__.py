EXTENSION_META = {
    "name":    "web",
    "version": "1.3",
    "description": (
        "Web tools: DuckDuckGo search, open_url (browser-render a page returning "
        "elements/text/html/screenshot), and Camoufox browser automation "
        "(click, type, extract, screenshot). "
        "Screenshots are saved to workspace/outputs/browser/ and returned inline. "
        "One Camoufox browser instance per agent session, shared by all browser tools."
    ),
    "default_config": {
        # True | False | "virtual" — "virtual" runs headful Firefox under Xvfb,
        # which is what survives bot checks; plain headless is easily detected.
        "headless":              "virtual",
        "timeout_ms":            30000,
        "settle_timeout_ms":     15000,
        # Screenshots above this are saved but not shown inline (context cost).
        "screenshot_max_bytes":  1500000,
        "wait_until":            "domcontentloaded",
        "shift_enter_for_newline": True,
        "ignore_tags":           ["script", "style"],
        "max_discovery_elements": 40,
        "browse_max_bytes":      2000000,
        "browse_max_chars":      20000,
        "browse_user_agent":     "TinyCTX/1.1",
        "prompt_priority":       12,
        "search_results":        5,
        "output_dir":            "outputs/browser",
    },
}

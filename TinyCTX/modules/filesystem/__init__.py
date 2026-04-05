EXTENSION_META = {
    "name":    "filesystem",
    "version": "3.0",
    "description": (
        "Core filesystem tools: shell, view, write_file, str_replace, grep, glob_search. "
        "grep wraps ripgrep (with Python fallback). glob_search finds files by pattern. "
        "view automatically converts files via registered handlers (pdf, images, etc)."
    ),
    "default_config": {
        "page_size":  2000,            # lines per view_range chunk
        "cache_size": 128,             # max cached file conversions
        "shell_timeout": 60,  # add this to prevent hanging on long-running shell commands
    },
}
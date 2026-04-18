EXTENSION_META = {
    "name":    "filesystem",
    "version": "3.1",
    "description": (
        "Core filesystem tools: view, write_file, edit_file, grep, glob_search. "
        "grep wraps ripgrep (with Python fallback). glob_search finds files by pattern. "
        "view renders images as vision blocks. "
        "Shell execution has moved to the shell module."
    ),
    "default_config": {
        "page_size":  2000,   # lines per view_range chunk
        "cache_size": 128,    # max cached file conversions
    },
}

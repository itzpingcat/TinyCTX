EXTENSION_META = {
    "name":    "filesystem",
    "version": "2.0",
    "description": (
        "Core filesystem tools modelled after Claude's own tool suite: "
        "bash, view, create_file, str_replace. "
        "view automatically converts files via registered handlers (pdf, images, etc)."
    ),
    "default_config": {
        "workspace":  "/home/agent",   # default working directory for bash
        "page_size":  2000,            # lines per view_range chunk
        "cache_size": 128,             # max cached file conversions
    },
}
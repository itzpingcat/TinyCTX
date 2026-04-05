EXTENSION_META = {
    "name":    "mcp",
    "version": "1.0",
    "description": (
        "MCP (Model Context Protocol) client. Connects to stdio MCP servers defined "
        "in config.yaml under mcp.servers, discovers their tools at startup, and "
        "registers them into the agent's tool_handler as mcp__<server>__<tool>. "
    ),
}
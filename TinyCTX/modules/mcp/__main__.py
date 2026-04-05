"""
modules/mcp/__main__.py

MCP (Model Context Protocol) client module.

Reads server definitions from config.yaml under the top-level `mcp:` key,
connects to each server over stdio at startup, and registers all discovered
tools into agent.tool_handler as:

    mcp__<server_name>__<tool_name>

Config (config.yaml):
---------------------
    mcp:
      servers:
        filesystem:
          command: npx
          args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
          env:
            SOME_VAR: value   # optional extra env vars (merged with os.environ)
          tools:
            read_file:    always_on   # always in the LLM's tool list
            write_file:   deferred    # available via tools_search (default)
            delete_file:  disabled    # never registered

        postgres:
          command: uvx
          args: ["mcp-server-postgres", "--db-url", "postgresql://localhost/mydb"]
          # No 'tools' block — all tools default to 'deferred'

        everything:
          command: npx
          args: ["-y", "@modelcontextprotocol/server-everything"]

Per-tool visibility (under servers.<name>.tools):
  always_on  — registered and immediately enabled (in every LLM call)
  deferred   — registered but not enabled; agent must call tools_search (default)
  disabled   — not registered at all

Each server's tools are namespaced as mcp__<server>__<tool> to avoid
collisions. The tool description passed to the LLM includes the original
server tool description so the model knows what each tool does.

Lifecycle:
----------
  - Servers are started once at register() time.
  - Each server gets its own persistent ClientSession held open for the
    agent's lifetime (stdio_client context managers kept alive via tasks).
  - On agent.reset(), all servers are stopped and restarted cleanly.
  - If a server fails to start or tool discovery fails, it is skipped with
    a warning — other servers continue to work normally.

Requires:
---------
    pip install mcp
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal server connection state
# ---------------------------------------------------------------------------

class _MCPServer:
    """Holds a live connection to one MCP stdio server."""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str]) -> None:
        self.name    = name
        self.command = command
        self.args    = args
        self.env     = env

        # Set after connect()
        self.session:    Any = None   # mcp.ClientSession
        self._cm_stack:  Any = None   # AsyncExitStack keeping contexts alive
        self.tools:      list[Any] = []  # mcp tool objects from list_tools()

    async def connect(self) -> None:
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        merged_env = {**os.environ, **self.env}

        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=merged_env,
        )

        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session     = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        self._cm_stack = stack
        self.session   = session

        result     = await session.list_tools()
        self.tools = result.tools
        logger.info(
            "[mcp] server '%s' connected — %d tool(s): %s",
            self.name,
            len(self.tools),
            ", ".join(t.name for t in self.tools),
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        result = await self.session.call_tool(tool_name, arguments)
        # result.content is a list of content blocks (TextContent, ImageContent, etc.)
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                # Fallback: JSON-encode non-text blocks
                parts.append(json.dumps(block.model_dump() if hasattr(block, "model_dump") else str(block)))
        return "\n".join(parts) if parts else "[no output]"

    async def stop(self) -> None:
        if self._cm_stack:
            try:
                await self._cm_stack.aclose()
            except Exception as exc:
                logger.debug("[mcp] error stopping server '%s': %s", self.name, exc)
            self._cm_stack = None
            self.session   = None
            self.tools     = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mcp_schema_to_json(tool) -> dict:
    """Convert an MCP tool's inputSchema to a JSON schema dict."""
    schema = tool.inputSchema
    if hasattr(schema, "model_dump"):
        return schema.model_dump()
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}


def _tool_fn_name(server_name: str, tool_name: str) -> str:
    """Canonical namespaced name used in tool_handler registration."""
    return f"mcp__{server_name}__{tool_name}"


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def register(agent) -> None:
    # ------------------------------------------------------------------ config
    raw_cfg: dict = {}
    if hasattr(agent.config, "mcp") and agent.config.mcp:
        raw_cfg = agent.config.mcp
    elif hasattr(agent.config, "_raw") and isinstance(agent.config._raw, dict):
        raw_cfg = agent.config._raw.get("mcp", {})
    else:
        # Walk the config object looking for an mcp attribute
        for attr in ("mcp", "_mcp", "extra"):
            val = getattr(agent.config, attr, None)
            if isinstance(val, dict) and "servers" in val:
                raw_cfg = val
                break

    servers_cfg: dict = raw_cfg.get("servers", {}) if isinstance(raw_cfg, dict) else {}

    if not servers_cfg:
        logger.info("[mcp] no servers configured — add an 'mcp.servers' block to config.yaml")
        return

    # ------------------------------------------------------------------ build server objects
    servers: list[_MCPServer] = []
    # Map server name -> per-tool visibility config dict
    tools_cfgs: dict[str, dict] = {}

    for name, cfg in servers_cfg.items():
        if not isinstance(cfg, dict):
            logger.warning("[mcp] server '%s' config is not a dict — skipping", name)
            continue
        command = cfg.get("command")
        if not command:
            logger.warning("[mcp] server '%s' missing 'command' — skipping", name)
            continue
        srv = _MCPServer(
            name=name,
            command=command,
            args=[str(a) for a in cfg.get("args", [])],
            env={str(k): str(v) for k, v in cfg.get("env", {}).items()},
        )
        servers.append(srv)
        tools_cfgs[name] = {str(k): str(v) for k, v in cfg.get("tools", {}).items()}

    # ------------------------------------------------------------------ startup
    async def _start_all() -> None:
        for srv in servers:
            try:
                await srv.connect()
                _register_server_tools(agent, srv, tools_cfgs.get(srv.name, {}))
            except ImportError:
                logger.error(
                    "[mcp] 'mcp' package not installed — run: pip install mcp"
                )
                return
            except Exception as exc:
                logger.warning("[mcp] server '%s' failed to start: %s", srv.name, exc)

    asyncio.get_event_loop().create_task(_start_all())

    # ------------------------------------------------------------------ reset hook
    original_reset = agent.reset

    def patched_reset() -> None:
        original_reset()

        async def _restart():
            for srv in servers:
                await srv.stop()
            # Unregister old tools then reconnect
            for srv in servers:
                for t in srv.tools:
                    fn_name = _tool_fn_name(srv.name, t.name)
                    agent.tool_handler.tools.pop(fn_name, None)
            await _start_all()

        try:
            asyncio.get_running_loop().create_task(_restart())
        except RuntimeError:
            pass

    agent.reset = patched_reset
    logger.info("[mcp] module registered — %d server(s) starting", len(servers))


# ---------------------------------------------------------------------------
# Per-server tool registration
# ---------------------------------------------------------------------------

# Sentinel set used to track which tool names were removed from tool_handler
# during a reset so _start_all can cleanly re-register them.
_VALID_VISIBILITY = frozenset({"always_on", "deferred", "disabled"})


def _resolve_visibility(tools_cfg: dict, tool_name: str) -> str:
    """
    Return the visibility for a specific tool name.
    tools_cfg maps tool_name -> "always_on" | "deferred" | "disabled".
    Defaults to "deferred" if not specified.
    """
    vis = str(tools_cfg.get(tool_name, "deferred")).lower().strip()
    if vis not in _VALID_VISIBILITY:
        logger.warning(
            "[mcp] unknown visibility '%s' for tool '%s' — defaulting to 'deferred'",
            vis, tool_name,
        )
        return "deferred"
    return vis


def _register_server_tools(agent, srv: _MCPServer, tools_cfg: dict) -> None:
    for tool in srv.tools:
        _register_one_tool(agent, srv, tool, tools_cfg)


def _register_one_tool(agent, srv: _MCPServer, tool, tools_cfg: dict) -> None:
    visibility = _resolve_visibility(tools_cfg, tool.name)

    if visibility == "disabled":
        logger.debug("[mcp] tool '%s.%s' disabled — skipping", srv.name, tool.name)
        return

    fn_name     = _tool_fn_name(srv.name, tool.name)
    description = tool.description or f"MCP tool '{tool.name}' from server '{srv.name}'"
    schema      = _mcp_schema_to_json(tool)
    properties  = schema.get("properties", {})
    required    = schema.get("required", [])

    # Build a dynamic async function. We can't use a simple lambda because
    # tool_handler.register_tool inspects the signature for schema generation.
    # Instead we register directly into tool_handler.tools with the schema
    # we already have from the MCP server — no introspection needed.

    async def _call(**kwargs: Any) -> str:
        try:
            return await srv.call_tool(tool.name, kwargs)
        except Exception as exc:
            return f"[mcp error: {exc}]"

    # Register bypassing auto-introspection — inject schema directly
    agent.tool_handler.tools[fn_name] = {
        "function":    _call,
        "description": description,
        "signature":   None,
        "properties":  {
            k: _prop_to_json_schema(v)
            for k, v in properties.items()
        },
        "required":    required,
    }

    if visibility == "always_on":
        agent.tool_handler.enabled.add(fn_name)
        logger.debug("[mcp] registered tool '%s' (always_on)", fn_name)
    else:
        logger.debug("[mcp] registered tool '%s' (deferred)", fn_name)


def _prop_to_json_schema(prop: dict) -> dict:
    """Normalise an MCP property definition to what tool_handler expects."""
    out: dict = {}
    if "type" in prop:
        out["type"] = prop["type"]
    else:
        out["type"] = "string"
    if "description" in prop:
        out["description"] = prop["description"]
    if "enum" in prop:
        out["enum"] = prop["enum"]
    return out
"""
modules/mcp

MCP (Model Context Protocol) client module.

Reads server definitions from config.yaml under the top-level `mcp:` key,
connects to each server over stdio once at process startup, and registers
all discovered tools into every AgentCycle's tool_handler as:

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
  - Servers are connected once, at @hook(HookType.STARTUP) — process
    lifetime, not per-cycle. (The pre-Module version reconnected every
    single AgentCycle, since register_agent() ran per cycle; that was waste,
    not a feature — nothing here reads per-turn state, only the tool
    closures need re-registering into each cycle's own fresh tool_handler,
    which @hook(HookType.TURN_START) below does cheaply against the
    already-connected servers.)
  - Each server gets its own persistent ClientSession held open for the
    process's lifetime (stdio_client context managers kept alive via a
    background task).
  - If a server fails to start or tool discovery fails, it is skipped with
    a warning — other servers continue to work normally.
  - There is no reset/reconnect lifecycle: the pre-Module version patched
    agent.reset() to restart servers, but AgentCycle has no reset() method
    (dead code — this would have raised AttributeError the moment it ran
    against a real AgentCycle; there is no test coverage for this module).
    Not carried forward.

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

from TinyCTX.decorators import hook
from TinyCTX.hooks import HookType
from TinyCTX.module import Module

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
        return "\n".join(parts) if parts else "No output"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_mcp_config(config) -> dict:
    if hasattr(config, "mcp") and config.mcp:
        return config.mcp
    if hasattr(config, "_raw") and isinstance(config._raw, dict):
        return config._raw.get("mcp", {})
    # Walk the config object looking for an mcp attribute
    for attr in ("mcp", "_mcp", "extra"):
        val = getattr(config, attr, None)
        if isinstance(val, dict) and "servers" in val:
            return val
    return {}


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


def _register_one_tool(tool_handler, srv: _MCPServer, tool, tools_cfg: dict) -> None:
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
            return f"MCP error: {exc}"

    # Register bypassing auto-introspection — inject schema directly
    tool_handler.tools[fn_name] = {
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
        tool_handler.enabled.add(fn_name)
        logger.debug("[mcp] registered tool '%s' (always_on)", fn_name)
    else:
        logger.debug("[mcp] registered tool '%s' (deferred)", fn_name)


class MCP(Module):
    """Connects to configured MCP stdio servers and registers their tools
    into every AgentCycle's tool_handler as mcp__<server>__<tool>."""

    def __init__(self) -> None:
        self._servers: list[_MCPServer] = []
        self._tools_cfgs: dict[str, dict] = {}

    @hook(HookType.STARTUP)
    def load(self, runtime) -> None:
        raw_cfg = _extract_mcp_config(runtime.config)
        servers_cfg: dict = raw_cfg.get("servers", {}) if isinstance(raw_cfg, dict) else {}

        if not servers_cfg:
            logger.info("[mcp] no servers configured — add an 'mcp.servers' block to config.yaml")
            return

        for name, cfg in servers_cfg.items():
            if not isinstance(cfg, dict):
                logger.warning("[mcp] server '%s' config is not a dict — skipping", name)
                continue
            command = cfg.get("command")
            if not command:
                logger.warning("[mcp] server '%s' missing 'command' — skipping", name)
                continue
            self._servers.append(_MCPServer(
                name=name,
                command=command,
                args=[str(a) for a in cfg.get("args", [])],
                env={str(k): str(v) for k, v in cfg.get("env", {}).items()},
            ))
            self._tools_cfgs[name] = {str(k): str(v) for k, v in cfg.get("tools", {}).items()}

        asyncio.get_event_loop().create_task(self._connect_all())
        logger.info("[mcp] module registered — %d server(s) starting", len(self._servers))

    async def _connect_all(self) -> None:
        for srv in self._servers:
            try:
                await srv.connect()
            except ImportError:
                logger.error("[mcp] 'mcp' package not installed — run: pip install mcp")
                return
            except Exception as exc:
                logger.warning("[mcp] server '%s' failed to start: %s", srv.name, exc)

    @hook(HookType.TURN_START)
    def wire_tools(self, cycle) -> None:
        # Cheap per-cycle step: register already-discovered tools (from the
        # one-time STARTUP connect) into THIS cycle's own tool_handler. A
        # server still connecting (or that failed) simply contributes no
        # tools yet/at all this cycle — no reconnect attempt here.
        for srv in self._servers:
            if not srv.tools:
                continue
            tools_cfg = self._tools_cfgs.get(srv.name, {})
            for tool in srv.tools:
                _register_one_tool(cycle.tool_handler, srv, tool, tools_cfg)

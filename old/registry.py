"""
tools/registry.py — Tool registry.
Holds tool schemas and handlers. Injected into AgentLoop at startup.
No LLM calls, no gateway imports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from contracts import ToolCall, ToolResult

logger = logging.getLogger(__name__)

Handler = Callable[[dict], str]


@dataclass
class ToolEntry:
    name:    str
    schema:  dict       # OpenAI-compatible JSON Schema tool definition
    handler: Handler    # fn(args: dict) -> str


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}

    def register(self, name: str, schema: dict, handler: Handler) -> None:
        self._tools[name] = ToolEntry(name=name, schema=schema, handler=handler)
        logger.debug("Registered tool '%s'", name)

    def schemas(self) -> list[dict]:
        """Return all tool schemas — passed to LLM on each inference call."""
        return [{"type": "function", "function": e.schema} for e in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        """Dispatch a ToolCall to its handler. Errors become ToolResult strings."""
        entry = self._tools.get(call.tool_name)
        if entry is None:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                output=f"[error: unknown tool '{call.tool_name}']",
                is_error=True,
            )
        try:
            output = entry.handler(call.args)
        except Exception as e:
            logger.exception("Tool '%s' raised", call.tool_name)
            return ToolResult(call_id=call.call_id, tool_name=call.tool_name,
                              output=f"[error: {e}]", is_error=True)

        return ToolResult(call_id=call.call_id, tool_name=call.tool_name, output=output)
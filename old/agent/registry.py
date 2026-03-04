from __future__ import annotations
from typing import Callable


class Registry:
    """
    Global registry shared across all sessions.
    Populated once at startup by extension register_global() calls.

    Holds:
      - tools         : LLM function-call tools (schema + handler)
      - file_handlers : convert raw file bytes to a string representation,
                        keyed by mime type or file extension.
                        Used exclusively by the filesystem extension's
                        read_file tool — nothing else should call these directly.
    """

    def __init__(self):
        # name -> {"schema": {...}, "handler": fn(args: dict) -> str}
        self._tools: dict[str, dict] = {}

        # mime or ".ext" -> fn(content: bytes, path: str) -> str
        self._file_handlers: dict[str, Callable] = {}

    # -----------------------------------------------------------------
    # Tools
    # -----------------------------------------------------------------

    def register_tool(self, name: str, schema: dict, handler: Callable):
        """
        Register an LLM-callable tool.

        schema  — OpenAI function schema:
                  {"name": ..., "description": ..., "parameters": {...}}
        handler — fn(args: dict) -> str
                  Must return a string; becomes the tool result in dialogue.
        """
        self._tools[name] = {"schema": schema, "handler": handler}

    def unregister_tool(self, name: str):
        self._tools.pop(name, None)

    def get_tool_schemas(self) -> list[dict]:
        return [
            {"type": "function", "function": t["schema"]}
            for t in self._tools.values()
        ]

    def get_tool_handler(self, name: str) -> Callable | None:
        entry = self._tools.get(name)
        return entry["handler"] if entry else None

    # -----------------------------------------------------------------
    # File handlers
    # -----------------------------------------------------------------

    def register_file_handler(self, key: str, handler: Callable):
        """
        Register a file content transformer.

        key     — mime type ("application/pdf") or extension (".pdf")
        handler — fn(content: bytes, path: str) -> str
                  Returns a plain string the LLM can read.

        Later registrations override earlier ones for the same key,
        so modules can replace built-in handlers.
        """
        self._file_handlers[key] = handler

    def get_file_handler(self, path: str, mime: str | None = None) -> Callable | None:
        """Return best handler for a file, checking mime type then extension."""
        if mime and mime in self._file_handlers:
            return self._file_handlers[mime]
        if "." in path:
            ext = "." + path.rsplit(".", 1)[-1].lower()
            return self._file_handlers.get(ext)
        return None
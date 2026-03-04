from __future__ import annotations
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agent.context import Context, USER, ASSISTANT, TOOL
from agent.registry import Registry


# ---------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------

class Session:
    """
    A session is a Context (state) bundled with methods to act on it.

    Identity:
      name    — human-readable label, e.g. "discord"
      version — integer, increments on reset. Folder: sessions/{name}/{version}.json
      id      — "{name}_{version}", unique key used by SessionManager

    Persistence:
      Saves/loads the raw dialogue log only.
      Prompt providers and hooks are re-registered by modules on load.

    Agentic loop:
      send(msg) -> adds user turn -> streams LLM -> executes tool calls
               -> streams LLM again -> ... -> done

    Bridge interface:
      on_message(fn) -> fn receives typed event dicts as the loop runs
    """

    def __init__(
        self,
        name: str,
        version: int,
        llm,
        registry: Registry,
        config: dict,
        sessions_dir: Path,
    ):
        self.name = name
        self.version = version
        self.id = f"{name}_{version}"

        self.llm = llm
        self.registry = registry
        self.cfg = config
        self.sessions_dir = sessions_dir

        self.context = Context(config=config)

        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at

        self._lock = threading.Lock()
        self._listeners: list[Callable] = []

    # -----------------------------------------------------------------
    # Bridge interface
    # -----------------------------------------------------------------

    def on_message(self, fn: Callable) -> Callable:
        """
        Register a listener for assistant output events.

        fn(event: dict) — event types:
          {"type": "content",     "delta": str}
          {"type": "tool_call",   "name": str, "args": dict}
          {"type": "tool_result", "name": str, "result": str}
          {"type": "done"}
          {"type": "error",       "message": str}

        All events include "session_id".
        Returns an unsubscribe callable.
        """
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def _emit(self, event: dict):
        event["session_id"] = self.id
        for fn in self._listeners:
            try:
                fn(event)
            except Exception as e:
                print(f"[session:{self.id}] listener error: {e}")

    # -----------------------------------------------------------------
    # Send
    # -----------------------------------------------------------------

    def send(self, user_message: str):
        """
        Add a user message and run the agentic loop to completion.
        Thread-safe — one loop at a time per session.

        Bridges call this for user input.
        modules (heartbeat, cron) call this for autonomous messages.
        """
        with self._lock:
            self.context.user(user_message)
            self._run_loop()
            self._save()

    def send_async(self, user_message: str) -> threading.Thread:
        """Non-blocking send(). Returns the thread."""
        t = threading.Thread(target=self.send, args=(user_message,), daemon=True)
        t.start()
        return t

    # -----------------------------------------------------------------
    # Agentic loop
    # -----------------------------------------------------------------

    def _run_loop(self):
        max_iterations = self.cfg.get("max_iterations", 20)
        tools = self.registry.get_tool_schemas()

        for _ in range(max_iterations):
            messages = self.context.assemble()

            self.llm.tools = tools or None

            content_buf = ""
            tool_call_buf: dict[int, dict] = {}

            for chunk in self.llm.query(messages):

                if chunk["type"] == "error":
                    self._emit({"type": "error", "message": chunk["delta"]})
                    return

                if chunk["type"] == "content":
                    content_buf += chunk["delta"]
                    self._emit({"type": "content", "delta": chunk["delta"]})

                if chunk["type"] == "tool_call":
                    self._accumulate_tool_call(tool_call_buf, chunk["delta"])

            tool_calls = self._finalise_tool_calls(tool_call_buf)

            # commit assistant turn
            if tool_calls:
                self.context.assistant(content_buf, tool_calls=tool_calls)
            elif content_buf.strip():
                self.context.assistant(content_buf)
                self._emit({"type": "done"})
                return
            else:
                # empty response
                self._emit({"type": "done"})
                return

            # execute tool calls sequentially
            for tc in tool_calls:
                self._emit({"type": "tool_call", "name": tc["name"], "args": tc["arguments"]})
                result = self._execute_tool(tc)
                self._emit({"type": "tool_result", "name": tc["name"], "result": result})
                self.context.tool(tc["id"], result)

        self._emit({"type": "error", "message": f"max_iterations ({max_iterations}) reached"})

    # -----------------------------------------------------------------
    # Tool call helpers
    # -----------------------------------------------------------------

    def _accumulate_tool_call(self, buf: dict, delta: dict):
        idx = delta.get("index", 0)
        if idx not in buf:
            buf[idx] = {"id": delta.get("id", str(uuid.uuid4())), "name": "", "arguments": ""}
        e = buf[idx]
        if delta.get("id"):
            e["id"] = delta["id"]
        fn = delta.get("function", {})
        if fn.get("name"):      e["name"]      += fn["name"]
        if fn.get("arguments"): e["arguments"] += fn["arguments"]

    def _finalise_tool_calls(self, buf: dict) -> list[dict]:
        result = []
        for idx in sorted(buf):
            tc = buf[idx]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": tc["arguments"]}
            result.append({"id": tc["id"], "name": tc["name"], "arguments": args})
        return result

    def _execute_tool(self, tc: dict) -> str:
        handler = self.registry.get_tool_handler(tc["name"])
        if not handler:
            return f"[error: no handler registered for tool '{tc['name']}']"
        try:
            return str(handler(tc["arguments"]))
        except Exception as e:
            return f"[error executing '{tc['name']}': {e}]"

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _session_path(self) -> Path:
        return self.sessions_dir / self.name / f"{self.version}.json"

    def _save(self):
        self.updated_at = datetime.now(timezone.utc).isoformat()
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "name":       self.name,
            "version":    self.version,
            "id":         self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dialogue":   self.context.dialogue,
        }
        path.write_text(json.dumps(data, indent=2))

    def _load(self, data: dict):
        """Restore dialogue from a previously saved dict. Called by SessionManager."""
        self.created_at = data.get("created_at", self.created_at)
        self.updated_at = data.get("updated_at", self.updated_at)
        self.context.dialogue = data.get("dialogue", [])
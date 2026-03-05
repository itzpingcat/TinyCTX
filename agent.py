"""
agent.py — The 6-stage agent execution loop.
One instance per session, owned by its Lane.
Yields OutboundReply chunks; never calls the gateway directly.

Stages:
  1. Intake           — add user message to Context
  2. Context Assembly — build message list via Context.assemble()
  3. Inference        — stream LLM, collect text + tool calls
  4. Tool Execution   — dispatch ToolCalls via tool_handler
  5. Result Backfill  — inject ToolResults back into Context
  6. Streaming Reply  — yield OutboundReply chunks to Lane

Module loading:
  On init, scans modules/ directory for packages exposing register(agent).
  Each module receives self and wires in whatever it needs — tools, prompt
  providers, context hooks. No hardcoding of module names anywhere.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

from contracts import InboundMessage, OutboundReply, ToolCall, ToolResult, SessionKey
from context import Context, HistoryEntry
from config import Config
from ai import LLM, TextDelta, ToolCallAssembled, LLMError
from utils.tool_handler import ToolCallHandler

logger = logging.getLogger(__name__)

# Where to look for modules. Relative to cwd (where main.py lives).
MODULES_DIR = Path("modules")


class AgentLoop:
    """
    Owns one session's Context, tool_handler, and LLM client.
    Called by Lane once per inbound message.
    Yields OutboundReply chunks that the Lane forwards to the gateway.
    """

    def __init__(self, session_key: SessionKey, config: Config) -> None:
        self.session_key  = session_key
        self.config       = config
        self.context      = Context()
        self.tool_handler = ToolCallHandler()
        self._turn_count  = 0
        self._llm         = LLM(
            base_url=config.llm.base_url,
            api_key=config.llm.api_key if _has_api_key(config) else "no-key",
            model=config.llm.model,
        )
        self._load_modules()

    # ------------------------------------------------------------------
    # Module loader
    # ------------------------------------------------------------------

    def _load_modules(self) -> None:
        """
        Scan MODULES_DIR for packages and call register(self) on each.
        A module is any subdirectory containing __main__.py or __init__.py
        that exposes a register() callable.
        Convention: register(agent) — receives self, wires in whatever it needs.
        """
        if not MODULES_DIR.exists():
            logger.debug("No modules/ directory found, skipping module load.")
            return

        for entry in sorted(MODULES_DIR.iterdir()):
            if not entry.is_dir():
                continue
            has_main = (entry / "__main__.py").exists()
            has_init = (entry / "__init__.py").exists()
            if not (has_main or has_init):
                continue

            module_name = f"modules.{entry.name}"
            try:
                for suffix in (".__main__", ""):
                    try:
                        mod = importlib.import_module(module_name + suffix)
                        if hasattr(mod, "register"):
                            break
                    except ModuleNotFoundError:
                        continue
                else:
                    logger.warning("Module '%s' has no register() — skipping", entry.name)
                    continue
                mod.register(self)
                logger.info("Loaded module '%s'", entry.name)
            except Exception:
                logger.exception("Failed to load module '%s'", entry.name)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, msg: InboundMessage) -> AsyncIterator[OutboundReply]:
        self._turn_count += 1
        logger.debug("[%s] turn %d", self.session_key, self._turn_count)

        # Stage 1: Intake
        self.context.add(HistoryEntry.user(msg.text))

        max_cycles = self.config.max_tool_cycles
        final_text = ""

        for cycle in range(max_cycles):
            # Stage 2: Context Assembly
            messages = self.context.assemble()

            # Stage 3: Inference
            text_chunks: list[str]      = []
            tool_calls:  list[ToolCall] = []
            error:       str | None     = None

            tools = self.tool_handler.get_tool_definitions() or None
            async for event in self._llm.stream(messages, tools=tools):
                if isinstance(event, TextDelta):
                    text_chunks.append(event.text)
                elif isinstance(event, ToolCallAssembled):
                    tool_calls.append(ToolCall(
                        call_id=event.call_id,
                        tool_name=event.tool_name,
                        args=event.args,
                    ))
                elif isinstance(event, LLMError):
                    error = event.message
                    break

            if error:
                logger.error("[%s] LLM error: %s", self.session_key, error)
                final_text = f"[LLM error: {error}]"
                break

            response_text = "".join(text_chunks)
            self.context.add(HistoryEntry.assistant(
                content=response_text,
                tool_calls=tool_calls if tool_calls else None,
            ))

            if not tool_calls:
                final_text = response_text
                break

            # Stages 4 & 5: Execute tools, backfill results
            logger.debug("[%s] cycle %d — %d tool call(s)", self.session_key, cycle, len(tool_calls))
            for tc in tool_calls:
                result = await self._execute_tool(tc)
                self.context.add(HistoryEntry.tool_result(result))

        else:
            logger.warning("[%s] hit max_tool_cycles (%d)", self.session_key, max_cycles)
            final_text = final_text or "[Tool cycle limit reached.]"

        # Stage 6: Streaming Reply
        async for chunk in self._stream_reply(final_text, msg):
            yield chunk

        await self._flush_history()

    # ------------------------------------------------------------------
    # Stage 4: Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, call: ToolCall) -> ToolResult:
        proxy = {
            "function": {"name": call.tool_name, "arguments": call.args},
            "id": call.call_id,
        }
        result = self.tool_handler.execute_tool_call(proxy)
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            output=str(result.get("result", result.get("error", "[no output]"))),
            is_error=not result.get("success", False),
        )

    # ------------------------------------------------------------------
    # Stage 6: Reply streaming
    # ------------------------------------------------------------------

    async def _stream_reply(self, text: str, source: InboundMessage) -> AsyncIterator[OutboundReply]:
        yield OutboundReply(
            session_key=self.session_key,
            text=text,
            reply_to_message_id=source.message_id,
            trace_id=source.trace_id,
            is_partial=False,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear conversation context. Called by /reset commands."""
        self.context.clear()
        self._turn_count = 0
        logger.info("[%s] context reset", self.session_key)

    async def _flush_history(self) -> None:
        """
        Persist the current dialogue to sessions/<session_key>/<version>.json.
        Version is auto-incremented — finds the highest existing N.json, writes N+1.
        Called automatically after every reply.
        """
        # sessions/dm_cli-owner/  or  sessions/group_discord_123456/
        safe_key = str(self.session_key).replace(":", "_")
        sessions_dir = Path("sessions") / safe_key
        sessions_dir.mkdir(parents=True, exist_ok=True)

        version = self._next_session_version(sessions_dir)
        path = sessions_dir / f"{version}.json"

        dialogue_raw = [
            {
                "id":           e.id,
                "role":         e.role,
                "content":      e.content,
                "tool_calls":   e.tool_calls,
                "tool_call_id": e.tool_call_id,
                "index":        e.index,
            }
            for e in self.context.dialogue
        ]

        data = {
            "session_key": str(self.session_key),
            "version":     version,
            "turn":        self._turn_count,
            "saved_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dialogue":    dialogue_raw,
        }

        try:
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug("[%s] flushed v%d -> %s", self.session_key, version, path)
        except Exception as exc:
            logger.warning("[%s] _flush_history failed: %s", self.session_key, exc)

    def _next_session_version(self, sessions_dir: Path) -> int:
        """Return the next unused integer filename (1, 2, 3, ...) in sessions_dir."""
        existing = [
            int(p.stem) for p in sessions_dir.glob("*.json") if p.stem.isdigit()
        ]
        return max(existing, default=0) + 1


def _has_api_key(config: Config) -> bool:
    return bool(os.environ.get(config.llm.api_key_env, "").strip())
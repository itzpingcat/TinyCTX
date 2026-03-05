"""
agent_loop.py — The 6-stage agent execution loop.
One instance per session, owned by its Lane.
Yields OutboundReply chunks; never calls the gateway directly.

Stages:
  1. Intake           — add user message to Context
  2. Context Assembly — build message list via Context.assemble()
  3. Inference        — stream LLM, collect text + tool calls
  4. Tool Execution   — dispatch ToolCalls, get ToolResults (STUBBED)
  5. Result Backfill  — inject ToolResults back into Context
  6. Streaming Reply  — yield OutboundReply chunks to Lane
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from contracts import InboundMessage, OutboundReply, ToolCall, ToolResult, SessionKey
from context import Context, HistoryEntry
from config import Config
from ai import LLM, TextDelta, ToolCallAssembled, LLMError
from utils.tool_handler import ToolCallHandler

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    Owns one session's Context and history.
    Called by Lane once per inbound message.
    Yields OutboundReply chunks that the Lane forwards to the gateway.
    """

    def __init__(self, session_key: SessionKey, config: Config, registry: ToolCallHandler | None = None) -> None:
        self.session_key = session_key
        self.config      = config
        self.registry: ToolCallHandler | None = registry
        self.context     = Context()
        self._turn_count = 0
        self._llm        = LLM(
            base_url=config.llm.base_url,
            api_key=config.llm.api_key if _has_api_key(config) else "no-key",
            model=config.llm.model,
        )

        self.context.register_prompt("agents_md", lambda ctx: "[AGENTS.md not yet loaded]", priority=0)
        self.context.register_prompt("soul_md",   lambda ctx: "[SOUL.md not yet loaded]",   priority=1)

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

            # Stage 3: Inference — stream LLM, collect events
            text_chunks: list[str]        = []
            tool_calls:  list[ToolCall]   = []
            error:       str | None       = None

            tools = self.registry.get_tool_definitions() if self.registry else None
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

            # Record assistant turn
            self.context.add(HistoryEntry.assistant(
                content=response_text,
                tool_calls=tool_calls if tool_calls else None,
            ))

            if not tool_calls:
                final_text = response_text
                break

            # Stages 4 & 5: Execute tools and backfill results
            logger.debug("[%s] cycle %d — %d tool call(s)", self.session_key, cycle, len(tool_calls))
            for tc in tool_calls:
                result = await self._execute_tool(tc)
                self.context.add(HistoryEntry.tool_result(result))

        else:
            logger.warning("[%s] hit max_tool_cycles (%d)", self.session_key, max_cycles)
            final_text = final_text or "[Tool cycle limit reached.]"

        # Stage 6: Streaming Reply — yield chunks to Lane
        async for chunk in self._stream_reply(final_text, msg):
            yield chunk

        await self._flush_history()

    # ------------------------------------------------------------------
    # Stage 4: Tool execution stub
    # ------------------------------------------------------------------

    async def _execute_tool(self, call: ToolCall) -> ToolResult:
        if not self.registry:
            return ToolResult(call_id=call.call_id, tool_name=call.tool_name,
                              output="[error: no tool handler]", is_error=True)
        # Adapt our ToolCall into the dict format ToolCallHandler expects
        proxy = {
            "function": {"name": call.tool_name, "arguments": call.args},
            "id": call.call_id,
        }
        result = self.registry.execute_tool_call(proxy)
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            output=str(result.get("result", result.get("error", "[no output]"))),
            is_error=not result.get("success", False),
        )

    # ------------------------------------------------------------------
    # Stage 6: Reply streaming
    # ------------------------------------------------------------------

    async def _stream_reply(
        self, text: str, source: InboundMessage
    ) -> AsyncIterator[OutboundReply]:
        # Single final chunk for now.
        # Real streaming: yield is_partial=True per token, then is_partial=False.
        yield OutboundReply(
            session_key=self.session_key,
            text=text,
            reply_to_message_id=source.message_id,
            trace_id=source.trace_id,
            is_partial=False,
        )

    # ------------------------------------------------------------------
    # History persistence stub
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear conversation context and history. Called by /reset commands."""
        self.context.clear()
        self._turn_count = 0
        logger.info("[%s] context reset", self.session_key)

    async def _flush_history(self) -> None:
        """STUB — replace with Markdown log write when memory layer is built."""
        logger.debug("[%s] _flush_history (STUB)", self.session_key)


def _has_api_key(config: Config) -> bool:
    """Check if the API key env var is set without raising."""
    import os
    return bool(os.environ.get(config.llm.api_key_env, "").strip())
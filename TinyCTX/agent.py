from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import AsyncIterator

from TinyCTX.contracts import (
    AgentError, AgentEvent, AgentTextChunk, AgentTextFinal,
    AgentThinkingChunk, AgentToolCall, AgentToolResult,
    ToolCall, ToolResult, IMAGE_BLOCK_PREFIX
)
from TinyCTX.context import Context, HistoryEntry, HOOK_PRE_ASSEMBLE_ASYNC
from TinyCTX.ai import LLM, TextDelta, ThinkingDelta, ToolCallAssembled, LLMError
from TinyCTX.utils.tool_handler import ToolCallHandler

logger = logging.getLogger(__name__)

class AgentCycle:
    """
    A single execution turn. 
    Initializes with core config, but waits until .run() to load DB and state.
    """

    def __init__(self, config, module_registry) -> None:
        self.config = config
        self.module_registry = module_registry
        self.trace_id = str(uuid.uuid4())
        
        # Post-turn hooks registered by modules via register_agent.
        # Called by runtime after run() completes, with the final tail_node_id.
        # Signature: async (tail_node_id: str) -> None
        self.post_turn_hooks: list = []

        # Resources initialized during .run()
        self.db = None
        self.context = None
        self.models: dict[str, LLM] = {}
        self.tool_handler = None
        self.caller = None        # User; set in run()

        # Extra events enqueued by tools (e.g. present()) to be yielded
        # immediately after the AgentToolResult for that tool call.
        self.outbound_events: list = []

    async def run(
        self,
        node_id: str,
        caller,
        abort_event: asyncio.Event | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if abort_event is None:
            abort_event = asyncio.Event()

        self.caller = caller

        # --- 1. Resource Setup (Lazy Loading) ---
        if not self.db:
            from TinyCTX.db import ConversationDB
            workspace = Path(self.config.workspace.path).expanduser().resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            self.db = ConversationDB(workspace / "agent.db")

        # Load session state (model choice, enabled tools, etc.)
        state, _ = self.db.load_session_state(node_id)
        
        # Build LLMs based on primary + fallbacks
        primary_name = state.get("model") or self.config.llm.primary
        model_chain = [primary_name] + list(self.config.llm.fallback)
        
        self.models = {
            name: self._build_llm(self.config.models[name])
            for name in model_chain if name in self.config.models
        }

        # Build Tools
        self.tool_handler = ToolCallHandler()
        self.tool_handler.register_tool(self.tool_handler.tools_search, always_on=True)
        enabled_tools = state.get("enabled_tools")
        if enabled_tools:
            for t in enabled_tools:
                if t in self.tool_handler.tools:
                    self.tool_handler.enabled.add(t)

        # Build Context
        primary_mc = self.config.models.get(primary_name)
        self.context = Context(
            db=self.db,
            tail_node_id=node_id,
            token_limit=self.config.context,
            image_tokens_per_block=getattr(primary_mc, "tokens_per_image", 280),
        )

        # Wire modules into this cycle turn
        self.module_registry.register_agent(self)

        # --- 2. Generation Loop ---
        # Tracker for metadata yielded in events
        meta = {
            "trace_id": self.trace_id,
            "reply_to_message_id": "synthetic",
            "tail_node_id": node_id
        }

        max_cycles = self.config.max_tool_cycles
        final_text = ""
        streaming_active = False
        agent_name: str | None = state.get("agent_name")

        for cycle_num in range(max_cycles):
            logger.debug("[agent] cycle %d, node %s", cycle_num + 1, node_id)
            if abort_event.is_set():
                yield AgentError(message="[aborted]", **meta)
                return

            # Context Assembly
            logger.debug("[agent] running async hooks")
            await self.context.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
            tools = self.tool_handler.get_tool_definitions(
                caller_level=self.caller.permission_level,
                minimal_tokens=self.config.permissions.minimal_tokens,
            ) or None
            messages, _ = self.context.assemble(tools=tools)
            logger.debug("[agent] assembled %d messages, starting inference", len(messages))

            # Inference with Fallback logic
            text_chunks, tool_calls_list, error = [], [], None
            async for _ev in self._stream_inference(messages, tools, model_chain, abort_event, meta):
                if isinstance(_ev, tuple):
                    # sentinel: (_chunks, _calls, _error)
                    text_chunks, tool_calls_list, error = _ev
                elif isinstance(_ev, AgentThinkingChunk):
                    # logger.debug("[agent] thinking chunk (%d chars)", len(_ev.text))
                    yield AgentThinkingChunk(text=_ev.text, **meta)
                elif isinstance(_ev, AgentTextChunk):
                    # logger.debug("[agent] text chunk (%d chars)", len(_ev.text))
                    streaming_active = True
                    yield AgentTextChunk(text=_ev.text, **meta)

            logger.debug("[agent] post-inference: error=%s tool_calls=%d", error, len(tool_calls_list))
            if error:
                yield AgentError(message=f"[LLM error: {error}]", **meta)
                return

            # Record Assistant response in Context
            response_text = "".join(text_chunks)
            self.context.add(HistoryEntry.assistant(
                content=response_text,
                tool_calls=tool_calls_list or None,
                author_id=agent_name,
            ))
            meta["tail_node_id"] = self.context.tail_node_id
            logger.debug("[agent] assistant node written, tail=%s", self.context.tail_node_id)

            if not tool_calls_list:
                final_text = response_text
                logger.debug("[agent] no tool calls, breaking loop")
                break

            # Tool Execution
            is_last_cycle = (cycle_num == max_cycles - 1)
            for tc in tool_calls_list:
                yield AgentToolCall(call_id=tc.call_id, tool_name=tc.tool_name, args=tc.args, **meta)
                
                result = await self._execute_tool(tc)
                
                if is_last_cycle:
                    result = ToolResult(
                        call_id=result.call_id,
                        tool_name=result.tool_name,
                        output="[Tool Limit Reached] Summarize now.",
                        is_error=True,
                    )

                self.context.add_tool_result(result)
                meta["tail_node_id"] = self.context.tail_node_id

                yield AgentToolResult(
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    output="[image]" if result.is_image else result.output,
                    is_error=result.is_error,
                    **meta
                )

                for extra in self.outbound_events:
                    yield extra
                self.outbound_events.clear()

        logger.debug("[agent] yielding AgentTextFinal, streaming_active=%s", streaming_active)
        # meta["tail_node_id"] is the real assistant tail — yield it as-is so
        # bridges can advance their cursor to the correct node.
        final_tail = meta["tail_node_id"]
        yield AgentTextFinal(text=final_text if not streaming_active else "", **meta)

        # Fire post-turn hooks registered by modules via register_agent.
        for hook in self.post_turn_hooks:
            logger.debug("[agent] running post-turn hook '%s'", getattr(hook, '__name__', hook))
            try:
                await hook(final_tail)
                logger.debug("[agent] post-turn hook '%s' done", getattr(hook, '__name__', hook))
            except Exception:
                logger.exception("[agent] post-turn hook '%s' raised", getattr(hook, '__name__', hook))

    # --- Internal Helpers ---

    def _build_llm(self, mc) -> LLM:
        return LLM(
            base_url=mc.base_url,
            api_key=getattr(mc, "api_key", "no-key"),
            model=mc.model,
            max_tokens=mc.max_tokens,
            temperature=mc.temperature,
        )

    async def _stream_inference(self, messages, tools, model_chain, abort_event, meta):
        """
        Async generator that yields raw AgentTextChunk / AgentThinkingChunk events
        (with placeholder ids — caller re-stamps them with current meta) as they
        stream, then yields a single tuple sentinel at the end:
            (chunks: list[str], tool_calls: list[ToolCall], error: str | None)
        The caller unpacks the tuple to get the final result.
        """
        for model_name in model_chain:
            llm = self.models[model_name]
            chunks: list[str] = []
            calls: list[ToolCall] = []
            error: str | None = None

            async for ev in llm.stream(messages, tools=tools):
                if abort_event.is_set():
                    yield ([], [], "aborted")
                    return

                if isinstance(ev, ThinkingDelta):
                    yield AgentThinkingChunk(text=ev.text,
                                             tail_node_id=meta["tail_node_id"],
                                             trace_id=meta["trace_id"],
                                             reply_to_message_id=meta["reply_to_message_id"])
                elif isinstance(ev, TextDelta):
                    chunks.append(ev.text)
                    yield AgentTextChunk(text=ev.text,
                                         tail_node_id=meta["tail_node_id"],
                                         trace_id=meta["trace_id"],
                                         reply_to_message_id=meta["reply_to_message_id"])
                elif isinstance(ev, ToolCallAssembled):
                    calls.append(ToolCall(ev.call_id, ev.tool_name, ev.args))
                elif isinstance(ev, LLMError):
                    error = ev.message
                    break

            if not error:
                yield (chunks, calls, None)
                return
            logger.warning("Model %s failed: %s", model_name, error)

        yield ([], [], error or "all models failed")

    async def _execute_tool(self, call: ToolCall) -> ToolResult:
        proxy = {
            "function": {"name": call.tool_name, "arguments": call.args},
            "id": call.call_id,
        }
        
        assert self.tool_handler is not None
        result = await self.tool_handler.execute_tool_call(proxy, caller=self.caller)
        raw_output = str(result.get("result", result.get("error", "[no output]")))
        
        # Determine if the tool failed based on the result flag or content analysis
        is_error = (not result.get("success", False)) or self._looks_like_failed_tool_output(raw_output)

        # --- vision unwrap ---
        # If the tool returned an IMAGE_BLOCK (e.g. from view()) and the model
        # supports vision, stash the data in ToolResult. OpenAI-compat servers 
        # don't support image content in tool-role messages, so we let the 
        # higher-level run() loop inject a follow-up user turn with the image.
        if not is_error and raw_output.startswith(IMAGE_BLOCK_PREFIX):
            try:
                payload = raw_output[len(IMAGE_BLOCK_PREFIX):]  # Format: "mime;base64data"
                sep = payload.index(";")
                mime = payload[:sep]
                b64data = payload[sep + 1:]

                primary_cfg = self.config.get_model_config(self.config.llm.primary)
                
                if primary_cfg.supports_vision:
                    return ToolResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        output=f"[{mime} — see attached image below]",
                        is_error=False,
                        is_image=True,
                        image_mime=mime,
                        image_b64=b64data,
                    )
                else:
                    # Fallback for non-vision models
                    raw_output = (
                        f"[Image file detected ({mime}) but the current model does not "
                        "support vision. Use a vision-capable model to inspect this file.]"
                    )
            except (ValueError, IndexError) as e:
                logger.error(f"[agent] Failed to parse image payload: {e}")
                is_error = True
                raw_output = f"Error parsing image block: {e}"

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            output=raw_output,
            is_error=is_error,
        )

    def _looks_like_failed_tool_output(self, text: str) -> bool:
        """Helper to catch common error strings in stdout."""
        lowered = text.lower()
        return any(x in lowered for x in ["traceback (most recent call last):", "exception: ", "error: "])
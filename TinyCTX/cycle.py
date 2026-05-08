"""
cycle.py — Sealed, stateless agent execution cycle.

AgentCycle owns exactly one turn of reasoning: it reads from the DB via
Context, streams from the LLM, executes tools, writes results back, and
yields AgentEvent objects. It holds no reference to Runtime and cannot
call runtime.push() or touch any module state.

CycleHooks carries the post-turn hook list. Runtime constructs AgentCycle
from its own fields inside _process(). Background cycles receive
CycleHooks(post_turn=[]) to prevent recursive chaining.

Stages (unchanged from agent.py):
  1. Intake           — add user message to Context  (skipped when msg is None)
  2. Context Assembly — await async hooks, then build message list
  3. Inference        — stream LLM, collect text + tool calls
  4. Tool Execution   — dispatch ToolCalls via tool_handler
  5. Result Backfill  — inject ToolResults back into Context
  6. Streaming Reply  — yield AgentEvent objects

Abort:
  abort_event is checked between every inference cycle and inside the LLM
  stream. If set, yields AgentError and exits cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from TinyCTX.contracts import (
    AgentError,
    AgentEvent,
    AgentTextChunk,
    AgentTextFinal,
    AgentThinkingChunk,
    AgentToolCall,
    AgentToolResult,
    IMAGE_BLOCK_PREFIX,
    InboundMessage,
    ToolCall,
    ToolResult,
)
from TinyCTX.context import Context, HistoryEntry, HOOK_PRE_ASSEMBLE_ASYNC
from TinyCTX.ai import LLM, TextDelta, ThinkingDelta, ToolCallAssembled, LLMError
from TinyCTX.utils.tool_handler import ToolCallHandler
from TinyCTX.utils.attachments import build_content_blocks

logger = logging.getLogger(__name__)

_EXIT_ERROR_RE = re.compile(r"(^|\n)\[exit \d+\](?=\n|$)")


def _looks_like_failed_tool_output(output: str) -> bool:
    lowered = (output or "").lstrip().lower()
    if (
        lowered.startswith("[error")
        or lowered.startswith("[blocked")
        or lowered.startswith("error:")
    ):
        return True
    return bool(_EXIT_ERROR_RE.search(output or ""))


# ---------------------------------------------------------------------------
# CycleHooks
# ---------------------------------------------------------------------------

@dataclass
class CycleHooks:
    """
    Hook lists injected into AgentCycle at construction time.

    post_turn:
        Called after AgentTextFinal is yielded, with the final tail_node_id.
        Signature: async (tail_node_id: str) -> None
        Background cycles receive an empty list to prevent recursive chaining.
    """
    post_turn: list[Callable[[str], Awaitable[None]]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AgentCycle
# ---------------------------------------------------------------------------

@dataclass
class AgentCycle:
    """
    One sealed, stateless turn of agent reasoning.

    All fields are supplied by the caller (Runtime._process). AgentCycle
    cannot reach back into Runtime — it only reads/writes via context and
    tool_handler, both of which are already wired to the correct DB and
    branch by the time the cycle is constructed.

    Fields
    ------
    tail_node_id    — DB cursor for this branch; context is already set_tail()'d here
    context         — fully wired Context (DB + tail set, async hooks registered)
    models          — pre-built LLM instances keyed by config name
    tool_handler    — ToolCallHandler with tools registered for this cycle
    config          — immutable Config snapshot
    abort_event     — asyncio.Event; checked between cycles and inside the stream
    permission_level — 0-100; controls which tools are available
    hooks           — post-turn hooks (empty for background cycles)
    message_id      — inbound message_id for reply_to attribution ("synthetic" if None)
    trace_id        — ties all events for one turn together
    max_tool_cycles — max inference+tool iterations before force-stop
    """
    tail_node_id:     str
    context:          Context
    models:           dict[str, LLM]
    tool_handler:     ToolCallHandler
    config:           object          # Config — avoid circular import at type-check time
    abort_event:      asyncio.Event
    permission_level: int
    hooks:            CycleHooks
    message_id:       str             = "synthetic"
    trace_id:         str             = field(default_factory=lambda: str(uuid.uuid4()))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        msg: InboundMessage | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """
        Execute one turn. Yields AgentEvent objects.

        msg=None  — synthetic turn: skip Stage 1 (used by background branches).
        msg=<msg> — normal turn: add user message to context, then generate.
        """
        is_synthetic = msg is None
        trace_id     = msg.trace_id   if msg is not None else self.trace_id
        msg_id       = msg.message_id if msg is not None else self.message_id

        # Shared kwargs for every event emitted this turn.
        # tail_node_id advances as the cycle writes nodes; we update it inline.
        ev = dict(
            tail_node_id         = self.tail_node_id,
            trace_id             = trace_id,
            reply_to_message_id  = msg_id,
        )

        # ------------------------------------------------------------------
        # Stage 1: Intake (skipped for synthetic turns)
        # ------------------------------------------------------------------
        if msg is not None:
            if msg.attachments:
                primary_cfg  = self.config.get_model_config(self.config.llm.primary)
                user_content = build_content_blocks(
                    text=msg.text,
                    attachments=msg.attachments,
                    model_cfg=primary_cfg,
                    att_cfg=self.config.attachments,
                    workspace=self.config.workspace.path,
                )
            else:
                user_content = str(msg.text) if msg.text is not None else ""
            self.context.add(HistoryEntry.user(
                user_content,
                author_id=msg.author.user_id,
            ))
            # Keep ev in sync with the tail after the user node is written.
            ev["tail_node_id"] = self.context.tail_node_id

        max_cycles       = self.config.max_tool_cycles
        final_text       = ""
        streaming_active = False

        for cycle in range(max_cycles):
            # Abort check between cycles
            if self.abort_event.is_set():
                logger.info("[%s] aborted before cycle %d", ev["tail_node_id"], cycle)
                yield AgentError(message="[generation aborted]", **ev)
                return

            # --------------------------------------------------------------
            # Stage 2: Context Assembly
            # --------------------------------------------------------------
            await self.context.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
            minimal_tokens = self.config.permissions.minimal_tokens
            tools    = self.tool_handler.get_tool_definitions(
                caller_level=self.permission_level,
                minimal_tokens=minimal_tokens,
            ) or None
            messages = self.context.assemble(tools=tools)

            # Token budget telemetry
            tokens_used  = int(self.context.state.get("tokens_used_pre_trim", 0) or 0)
            active_tokens = int(self.context.state.get("tokens_used", 0) or 0)
            token_limit   = self.config.context
            token_pct     = tokens_used / token_limit if token_limit else 0
            if token_pct >= 0.80:
                logger.info(
                    "[cursor=%s] context at %.0f%% of token budget (%d/%d, active=%d)",
                    ev["tail_node_id"], token_pct * 100,
                    tokens_used, token_limit, active_tokens,
                )

            # --------------------------------------------------------------
            # Stage 3: Inference — walk primary → fallback chain
            # --------------------------------------------------------------
            text_chunks:      list[str]      = []
            tool_calls:       list[ToolCall] = []
            error:            str | None     = None
            streaming_active                 = False
            last_http_status: int | None     = None

            model_chain = [self.config.llm.primary] + list(self.config.llm.fallback)

            for model_name in model_chain:
                llm              = self.models[model_name]
                text_chunks      = []
                tool_calls       = []
                error            = None
                streaming_active = False
                last_http_status = None

                async for llm_event in llm.stream(messages, tools=tools):
                    if self.abort_event.is_set():
                        logger.info("[%s] aborted mid-stream", ev["tail_node_id"])
                        yield AgentError(message="[generation aborted]", **ev)
                        return

                    if isinstance(llm_event, ThinkingDelta):
                        yield AgentThinkingChunk(text=llm_event.text, **ev)

                    elif isinstance(llm_event, TextDelta):
                        text_chunks.append(llm_event.text)
                        if not tool_calls:
                            streaming_active = True
                            yield AgentTextChunk(text=llm_event.text, **ev)

                    elif isinstance(llm_event, ToolCallAssembled):
                        tool_calls.append(ToolCall(
                            call_id=llm_event.call_id,
                            tool_name=llm_event.tool_name,
                            args=llm_event.args,
                        ))

                    elif isinstance(llm_event, LLMError):
                        error = llm_event.message
                        if llm_event.message.startswith("HTTP "):
                            try:
                                last_http_status = int(llm_event.message.split()[1].rstrip(":"))
                            except (IndexError, ValueError):
                                pass
                        break

                if not error:
                    if model_name != self.config.llm.primary:
                        logger.info(
                            "[cursor=%s] inference succeeded on fallback model '%s'",
                            ev["tail_node_id"], model_name,
                        )
                        mc = self.config.models.get(model_name)
                        self.context.set_image_tokens(mc.tokens_per_image if mc else None)
                    break

                fo = self.config.llm.fallback_on
                should_fallback = fo.any_error or (
                    last_http_status is not None and last_http_status in fo.http_codes
                )
                if should_fallback and model_name != model_chain[-1]:
                    logger.warning(
                        "[cursor=%s] model '%s' failed (%s) — trying next fallback",
                        ev["tail_node_id"], model_name, error,
                    )
                    continue
                break

            if error:
                logger.error(
                    "[cursor=%s] LLM error (all models exhausted): %s",
                    ev["tail_node_id"], error,
                )
                yield AgentError(message=f"[LLM error: {error}]", **ev)
                return

            response_text = "".join(text_chunks)
            self.context.add(HistoryEntry.assistant(
                content=response_text,
                tool_calls=tool_calls if tool_calls else None,
            ))
            ev["tail_node_id"] = self.context.tail_node_id

            if not tool_calls:
                final_text = response_text
                break

            # --------------------------------------------------------------
            # Stages 4 & 5: Tool execution + result backfill
            # --------------------------------------------------------------
            logger.debug(
                "[%s] cycle %d — %d tool call(s)",
                ev["tail_node_id"], cycle, len(tool_calls),
            )

            is_last_cycle = (cycle == max_cycles - 1)
            if is_last_cycle:
                logger.warning(
                    "[cursor=%s] tool limit warning — cycle %d of %d",
                    ev["tail_node_id"], cycle, max_cycles,
                )

            for tc_idx, tc in enumerate(tool_calls):
                yield AgentToolCall(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    args=tc.args,
                    **ev,
                )
                result = await self._execute_tool(tc)
                if is_last_cycle and tc_idx == len(tool_calls) - 1:
                    result = ToolResult(
                        call_id=result.call_id,
                        tool_name=result.tool_name,
                        output=(
                            "[Tool Limit Reached] You have used the maximum number of "
                            "tool calls for this turn. Do not call any more tools — "
                            "write your final reply now."
                        ),
                        is_error=True,
                    )
                self.context.add(HistoryEntry.tool_result(result))
                ev["tail_node_id"] = self.context.tail_node_id

                # For image results, inject a follow-up user message with the
                # image_url block. OpenAI-compat servers don't support list content
                # in tool result messages, so a synthetic user turn is used instead.
                if result.is_image:
                    image_content = [
                        {"type": "text",      "text": "Here is the image from the tool result:"},
                        {"type": "image_url", "image_url": {"url": f"data:{result.image_mime};base64,{result.image_b64}"}},
                    ]
                    self.context.add(HistoryEntry.user(image_content))
                    ev["tail_node_id"] = self.context.tail_node_id

                display_output = f"[image: {result.image_mime}]" if result.is_image else result.output
                yield AgentToolResult(
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    output=display_output,
                    is_error=result.is_error,
                    **ev,
                )

        else:
            logger.warning(
                "[cursor=%s] hit max_tool_cycles (%d)",
                ev["tail_node_id"], max_cycles,
            )
            final_text = final_text or "[Tool cycle limit reached.]"

        # Stage 6: final event
        yield AgentTextFinal(
            text=final_text if not streaming_active else "",
            **ev,
        )

        # Post-turn hooks (skipped for synthetic turns — they are background work
        # themselves and must not spawn further background branches).
        if not is_synthetic:
            await self._fire_post_turn_hooks(ev["tail_node_id"])

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, call: ToolCall) -> ToolResult:
        proxy = {
            "function": {"name": call.tool_name, "arguments": call.args},
            "id": call.call_id,
        }
        result   = await self.tool_handler.execute_tool_call(proxy, caller_level=self.permission_level)
        raw_output = str(result.get("result", result.get("error", "[no output]")))
        is_error   = (not result.get("success", False)) or _looks_like_failed_tool_output(raw_output)

        # Vision unwrap: if view() returned an IMAGE_BLOCK sentinel and the
        # primary model supports vision, stash image data in ToolResult so
        # run() can inject a follow-up user message with the image_url block.
        if not is_error and raw_output.startswith(IMAGE_BLOCK_PREFIX):
            payload = raw_output[len(IMAGE_BLOCK_PREFIX):]
            sep     = payload.index(";")
            mime    = payload[:sep]
            b64data = payload[sep + 1:]

            primary_cfg = self.config.get_model_config(self.config.llm.primary)
            if primary_cfg.supports_vision:
                return ToolResult(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    output=f"[image/{mime} — see attached image below]",
                    is_error=False,
                    is_image=True,
                    image_mime=mime,
                    image_b64=b64data,
                )
            else:
                raw_output = (
                    f"[Image file detected ({mime}) but the current model does not "
                    "support vision. Use a vision-capable model to inspect this file.]"
                )

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            output=raw_output,
            is_error=is_error,
        )

    # ------------------------------------------------------------------
    # Post-turn hooks
    # ------------------------------------------------------------------

    async def _fire_post_turn_hooks(self, tail_node_id: str) -> None:
        """Invoke all post-turn hooks sequentially. Exceptions are logged, not raised."""
        for fn in self.hooks.post_turn:
            try:
                await fn(tail_node_id)
            except Exception:
                logger.exception(
                    "[post_turn] hook '%s' raised", getattr(fn, "__name__", fn)
                )

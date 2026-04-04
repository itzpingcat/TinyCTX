"""
agent.py — The 6-stage agent execution loop.
One instance per session, owned by its Lane.
Yields AgentEvent objects; never calls the gateway directly.

Stages:
  1. Intake           — add user message to Context  (skipped when msg is None)
  2. Context Assembly — await async hooks, then build message list via Context.assemble()
  3. Inference        — stream LLM, collect text + tool calls
  4. Tool Execution   — dispatch ToolCalls via tool_handler
  5. Result Backfill  — inject ToolResults back into Context
  6. Streaming Reply  — yield AgentEvent objects to Lane

Streaming behaviour:
  Tool-call cycles buffer text. The final cycle streams AgentTextChunk events
  live with a closing AgentTextFinal. Tool-use cycles yield AgentToolCall /
  AgentToolResult so bridges can display tool activity live.

Abort:
  Lane passes its abort_event to run(). The loop checks it between every
  inference cycle and inside the LLM stream. If set, yields AgentError and
  exits cleanly.

Tree refactor (Phase 2):
  AgentLoop is initialised with tail_node_id (str cursor) instead of
  session_key. The cursor points at the DB branch this agent is running on.
  All context.add() calls write immediately to the DB; cursor is kept in sync
  on disk after every node write.

Background branches (Phase 3):
  Modules register post-turn hooks via register_background_hook(fn). After
  AgentTextFinal is yielded, each hook receives the current tail_node_id and
  the agent's config. Hooks run as detached asyncio tasks — they must not
  block the caller. The canonical use is memory consolidation: create an
  opening node off the current tail via db.add_node(), construct a new
  AgentLoop pointing at it, and run it to completion.

  _run_background(tail_node_id, config) — module-level helper that fires a
  standalone AgentLoop in a detached task and discards all events.

Model pool:
  All named chat models from config.models are pre-instantiated.
  Inference walks primary → fallback list per config.llm.fallback_on.
  Modules call agent.get_model(name) to get a named LLM instance.

Module loading:
  Scans modules/ for packages exposing register(agent). No hardcoding.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import uuid
from pathlib import Path
from typing import AsyncIterator, Callable, Awaitable

from contracts import (
    InboundMessage, AgentEvent,
    AgentThinkingChunk, AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError,
    ToolCall, ToolResult, IMAGE_BLOCK_PREFIX,
)
from context import Context, HistoryEntry, HOOK_PRE_ASSEMBLE_ASYNC
from config import Config, ModelConfig
from ai import LLM, TextDelta, ThinkingDelta, ToolCallAssembled, LLMError
from utils.tool_handler import ToolCallHandler
from utils.attachments import build_content_blocks
from utils.commands import CommandRegistry
from db import ConversationDB

logger = logging.getLogger(__name__)

MODULES_DIR = Path("modules")
_EXIT_ERROR_RE = re.compile(r"(^|\n)\[exit \d+\](?=\n|$)")

def _looks_like_failed_tool_output(output: str) -> bool:
    lowered = (output or "").lstrip().lower()
    if lowered.startswith("[error") or lowered.startswith("[blocked") or lowered.startswith("error:"):
        return True
    return bool(_EXIT_ERROR_RE.search(output or ""))


def _build_llm(cfg: ModelConfig) -> LLM:
    try:
        api_key = cfg.api_key
    except EnvironmentError:
        api_key = "no-key"
    return LLM(
        base_url=cfg.base_url,
        api_key=api_key,
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        budget_tokens=cfg.budget_tokens,
        reasoning_effort=cfg.reasoning_effort,
        cache_prompts=cfg.cache_prompts,
    )


async def _run_background(tail_node_id: str, config: Config) -> None:
    """
    Run a standalone AgentLoop on tail_node_id as a synthetic turn, discarding
    all events. Called as a detached asyncio task — never awaited by the caller.

    Usage (from a background hook):
        opening = db.add_node(parent_id=current_tail, role="user", content="...")
        asyncio.create_task(_run_background(opening.id, agent.config))
    """
    try:
        loop = AgentLoop(tail_node_id=tail_node_id, config=config, is_subagent=True)
        async for _ in loop.run(msg=None):
            pass  # events discarded
    except Exception:
        logger.exception("[background] AgentLoop on tail=%s raised", tail_node_id)


class AgentLoop:
    def __init__(self, tail_node_id: str, config: Config, *, is_subagent: bool = False) -> None:
        self.tail_node_id  = tail_node_id  # cursor — the DB node this agent runs from
        self.lane_node_id  = tail_node_id  # original lane key — never changes
        self.config        = config
        self.is_subagent   = is_subagent
        primary_mc         = config.models.get(config.llm.primary)
        self.context       = Context(
            token_limit=config.context,
            image_tokens_per_block=primary_mc.tokens_per_image if primary_mc else 280,
        )
        self.tool_handler  = ToolCallHandler()
        self.commands      = CommandRegistry()  # replaced by Lane with the shared router registry
        self._turn_count   = 0
        self.gateway       = None  # set by Lane after construction

        # Open the shared agent.db and wire the cursor in.
        self._db           = self._open_db()
        self._tail_node_id = tail_node_id
        self.context.set_db(self._db)
        self.context.set_tail(self._tail_node_id)
        self.context.set_cursor_callback(self._on_context_tail_advance)

        # Background hooks: called after AgentTextFinal with (tail_node_id, config).
        self._background_hooks: list[Callable[[str, Config], Awaitable[None]]] = []

        self._models: dict[str, LLM] = {
            name: _build_llm(mc)
            for name, mc in config.models.items()
            if not mc.is_embedding
        }

        self.tool_handler.register_tool(self.tool_handler.tools_search, always_on=True)
        # NOTE: _load_modules() is NOT called here. Lane.__post_init__ wires
        # self.commands = router.commands first, then calls load_modules() so
        # modules register their commands into the shared registry.
        # Exception: subagent loops (background branches) have no Lane and
        # don't need commands — they load modules immediately.
        if is_subagent:
            self.load_modules()

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------

    def _open_db(self) -> ConversationDB:
        """Open (or create) agent.db in the workspace directory."""
        workspace = Path(self.config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return ConversationDB(workspace / "agent.db")

    def _on_context_tail_advance(self) -> None:
        """
        Called by Context.add() each time the tail node advances.
        Keeps _tail_node_id in sync with the context so callers reading
        agent._tail_node_id always see the latest cursor.
        """
        self._tail_node_id = self.context.tail_node_id
        # Propagate the updated cursor back to the public attribute.
        self.tail_node_id  = self._tail_node_id

    # ------------------------------------------------------------------
    # Public model accessor (for modules)
    # ------------------------------------------------------------------

    def get_model(self, name: str) -> LLM:
        if name in self._models:
            return self._models[name]
        primary = self.config.llm.primary
        logger.warning("get_model('%s') — not found, falling back to primary '%s'", name, primary)
        return self._models[primary]

    # ------------------------------------------------------------------
    # Background hook registry (Phase 3)
    # ------------------------------------------------------------------

    def register_background_hook(
        self, fn: Callable[[str, Config], Awaitable[None]]
    ) -> None:
        """Register an async hook called after each turn with (tail_node_id, config)."""
        self._background_hooks.append(fn)

    async def _fire_background_hooks(self, tail_node_id: str) -> None:
        """Invoke all registered background hooks after a turn completes."""
        for fn in self._background_hooks:
            try:
                await fn(tail_node_id, self.config)
            except Exception:
                logger.exception("[background] hook '%s' raised", getattr(fn, "__name__", fn))

    # ------------------------------------------------------------------
    # Module loader
    # ------------------------------------------------------------------

    def load_modules(self) -> None:
        if not MODULES_DIR.exists():
            return
        for entry in sorted(MODULES_DIR.iterdir()):
            if not entry.is_dir():
                continue
            if not ((entry / "__main__.py").exists() or (entry / "__init__.py").exists()):
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

    async def run(
        self,
        msg: InboundMessage | None = None,
        abort_event: asyncio.Event | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """
        Run one agent turn.

        msg=None  — synthetic turn: skip Stage 1, generate against current
                    context as-is (used by PUT /v1/sessions/{id}/generation).
        msg=<msg> — normal turn: add user message to context, then generate.

        After AgentTextFinal is yielded, any registered background hooks are
        fired (Phase 3). Synthetic turns do not trigger background hooks —
        they are themselves background work and should not spawn further
        background branches.
        """
        self._turn_count += 1
        is_synthetic = msg is None
        logger.debug("[cursor=%s] turn %d (synthetic=%s)", self._tail_node_id, self._turn_count, is_synthetic)

        if msg is not None:
            trace_id = msg.trace_id
            msg_id   = msg.message_id
        else:
            trace_id = str(uuid.uuid4())
            msg_id   = "synthetic"

        ev = dict(
            tail_node_id=self._tail_node_id,
            lane_node_id=self.lane_node_id,
            trace_id=trace_id,
            reply_to_message_id=msg_id,
        )

        # Stage 1: Intake (skipped for synthetic turns)
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
                user_content = msg.text
            self.context.add(HistoryEntry.user(user_content))

        max_cycles       = self.config.max_tool_cycles
        final_text       = ""
        streaming_active = False

        for cycle in range(max_cycles):
            # Abort check between cycles
            if abort_event and abort_event.is_set():
                logger.info("[%s] aborted before cycle %d", self._tail_node_id, cycle)
                yield AgentError(message="[generation aborted]", **ev)
                return

            # Stage 2: Context Assembly
            await self.context.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
            tools    = self.tool_handler.get_tool_definitions() or None
            messages = self.context.assemble(tools=tools)

            # Token budget telemetry
            tokens_used = int(self.context.state.get("tokens_used_pre_trim", 0) or 0)
            active_tokens = int(self.context.state.get("tokens_used", 0) or 0)
            token_limit = self.config.context
            token_pct   = tokens_used / token_limit if token_limit else 0
            if token_pct >= 0.80:
                logger.info(
                    "[cursor=%s] context at %.0f%% of token budget (%d/%d, active=%d)",
                    self._tail_node_id, token_pct * 100, tokens_used, token_limit, active_tokens,
                )

            # Stage 3: Inference — walk primary → fallback chain
            text_chunks:      list[str]      = []
            tool_calls:       list[ToolCall] = []
            error:            str | None     = None
            streaming_active                 = False
            last_http_status: int | None     = None

            model_chain = [self.config.llm.primary] + list(self.config.llm.fallback)

            for model_name in model_chain:
                llm              = self._models[model_name]
                text_chunks      = []
                tool_calls       = []
                error            = None
                streaming_active = False
                last_http_status = None

                async for llm_event in llm.stream(messages, tools=tools):
                    if abort_event and abort_event.is_set():
                        logger.info("[%s] aborted mid-stream", self._tail_node_id)
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
                            self._tail_node_id, model_name,
                        )
                        # Sync image token cost to the model that actually ran.
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
                        self._tail_node_id, model_name, error,
                    )
                    continue
                break

            if error:
                logger.error("[cursor=%s] LLM error (all models exhausted): %s", self._tail_node_id, error)
                yield AgentError(message=f"[LLM error: {error}]", **ev)
                return

            response_text = "".join(text_chunks)
            self.context.add(HistoryEntry.assistant(
                content=response_text,
                tool_calls=tool_calls if tool_calls else None,
            ))

            if not tool_calls:
                final_text = response_text
                break

            # Stages 4 & 5: Tool execution + result backfill
            logger.debug("[%s] cycle %d — %d tool call(s)", self._tail_node_id, cycle, len(tool_calls))
            for tc in tool_calls:
                yield AgentToolCall(call_id=tc.call_id, tool_name=tc.tool_name, args=tc.args, **ev)
                result = await self._execute_tool(tc)
                self.context.add(HistoryEntry.tool_result(result))
                # For image results, inject a follow-up user message with the image_url
                # block.  OpenAI-compat servers don't support list content in tool result
                # messages, so a synthetic user turn is a shitty but ok workaround.
                if result.is_image:
                    image_content = [
                        {"type": "text",      "text": "Here is the image from the tool result:"},
                        {"type": "image_url", "image_url": {"url": f"data:{result.image_mime};base64,{result.image_b64}"}},
                    ]
                    self.context.add(HistoryEntry.user(image_content))
                # For bridge display, show a tidy label instead of raw base64.
                display_output = f"[image: {result.image_mime}]" if result.is_image else result.output
                yield AgentToolResult(
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    output=display_output,
                    is_error=result.is_error,
                    **ev,
                )

        else:
            logger.warning("[cursor=%s] hit max_tool_cycles (%d)", self._tail_node_id, max_cycles)
            final_text = final_text or "[Tool cycle limit reached.]"

        yield AgentTextFinal(text=final_text if not streaming_active else "", **ev)

        # Fire background hooks after the turn completes.
        # Skipped for synthetic turns — they are themselves background work.
        if not is_synthetic and self._background_hooks:
            await self._fire_background_hooks(self._tail_node_id)

    # ------------------------------------------------------------------
    # Background branch runner
    # ------------------------------------------------------------------

    async def _run_background(self, tail_node_id: str) -> None:
        """
        Run a synthetic agent turn on a detached branch. Events are discarded.
        The branch writes its own nodes into agent.db; the caller's cursor is
        never touched.
        """
        logger.debug("[background] starting branch tail=%s", tail_node_id)
        try:
            loop = AgentLoop(tail_node_id=tail_node_id, config=self.config, is_subagent=True)
            async for _ in loop.run(msg=None):
                pass  # discard events
            logger.debug("[background] branch complete tail=%s", tail_node_id)
        except Exception:
            logger.exception("[background] branch failed tail=%s", tail_node_id)

    def queue_background_branch(self, tail_node_id: str) -> None:
        """
        Deprecated — call asyncio.create_task(_run_background(node_id, config)) directly.
        Kept temporarily so old call sites fail loudly rather than silently.
        """
        raise RuntimeError(
            "queue_background_branch() is removed. "
            "Use asyncio.create_task(_run_background(node_id, agent.config)) directly."
        )

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(
        self,
        call: ToolCall
    ) -> ToolResult:
        cache_key = None

        proxy = {
            "function": {"name": call.tool_name, "arguments": call.args},
            "id": call.call_id,
        }
        result = await self.tool_handler.execute_tool_call(proxy)
        raw_output = str(result.get("result", result.get("error", "[no output]")))
        is_error    = (not result.get("success", False)) or _looks_like_failed_tool_output(raw_output)

        # --- vision unwrap ---
        # If view() returned an IMAGE_BLOCK sentinel and the primary model
        # supports vision, stash the image data in the ToolResult so the
        # caller (run()) can inject a follow-up user message containing the
        # image_url block.  OpenAI-compat servers don't support list content
        # in tool result messages, so we use a separate user turn instead.
        if not is_error and raw_output.startswith(IMAGE_BLOCK_PREFIX):
            payload = raw_output[len(IMAGE_BLOCK_PREFIX):]  # "mime;base64data"
            sep = payload.index(";")
            mime    = payload[:sep]
            b64data = payload[sep + 1:]

            primary_cfg = self.config.get_model_config(self.config.llm.primary)
            if primary_cfg.supports_vision:
                tool_result = ToolResult(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    output=f"[image/{mime} — see attached image below]",
                    is_error=False,
                    is_image=True,
                    image_mime=mime,
                    image_b64=b64data,
                )
                return tool_result
            else:
                # Model doesn't support vision — return a friendly stub.
                raw_output = (
                    f"[Image file detected ({mime}) but the current model does not "
                    "support vision. Use a vision-capable model to inspect this file.]"
                )

        tool_result = ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            output=raw_output,
            is_error=is_error,
        )
        return tool_result

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        Clear in-memory context. Does NOT touch agent.db — the tree is
        permanent. The cursor stays at the current tail; the agent will
        continue to see prior history when it next assembles context.
        """
        self.context.clear()
        self._turn_count = 0
        logger.info("[cursor=%s] reset (in-memory only — tree intact)", self._tail_node_id)

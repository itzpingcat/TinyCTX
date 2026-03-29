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

Tree refactor (Phase 1):
  AgentLoop now opens agent.db (via ConversationDB) and wires it into Context.
  _tail_node_id tracks the current branch cursor. On first start for a
  session, a child of the global root is created as the session's home node.
  All context.add() calls write immediately to the DB; no end-of-turn flush.
  _flush_history, _restore_history, _load_latest_version, and next_session
  are deleted. reset() clears in-memory state only — it does not touch the DB.

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
import uuid
from pathlib import Path
from typing import AsyncIterator

from contracts import (
    InboundMessage, AgentEvent,
    AgentThinkingChunk, AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError,
    ToolCall, ToolResult, SessionKey,
)
from context import Context, HistoryEntry, HOOK_PRE_ASSEMBLE_ASYNC
from config import Config, ModelConfig
from ai import LLM, TextDelta, ThinkingDelta, ToolCallAssembled, LLMError
from utils.tool_handler import ToolCallHandler
from utils.attachments import build_content_blocks
from db import ConversationDB

logger = logging.getLogger(__name__)

MODULES_DIR = Path("modules")


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


class AgentLoop:
    def __init__(self, session_key: SessionKey, config: Config, version_override: int | None = None) -> None:
        self.session_key   = session_key
        self.config        = config
        self.context       = Context(token_limit=config.context)
        self.tool_handler  = ToolCallHandler()
        self._turn_count   = 0
        self.gateway       = None  # set by Lane after construction

        # Tree refactor: open DB and restore cursor
        self._db, self._tail_node_id = self._init_db()
        self.context.set_db(self._db)
        self.context.set_tail(self._tail_node_id)
        self.context.set_cursor_callback(self._on_context_tail_advance)

        self._models: dict[str, LLM] = {
            name: _build_llm(mc)
            for name, mc in config.models.items()
            if not mc.is_embedding
        }

        self.tool_handler.register_tool(self.tool_handler.tools_search, always_on=True)
        self._load_modules()

    # ------------------------------------------------------------------
    # DB initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> tuple[ConversationDB, str]:
        """
        Open (or create) agent.db in the workspace. Return (db, tail_node_id).

        The tail_node_id is persisted in a small cursor file alongside the DB
        so the session resumes where it left off on restart. The cursor file
        is named after the session_key to allow multiple sessions to share one
        DB (as they will once Phase 2 bridges are migrated).

        On first start for this session_key, a child of the global root is
        created as the session's initial node and becomes the cursor.
        """
        workspace = Path(self.config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        db_path = workspace / "agent.db"

        db = ConversationDB(db_path)

        # Cursor file: workspace/cursors/<safe_key>
        cursors_dir = workspace / "cursors"
        cursors_dir.mkdir(parents=True, exist_ok=True)
        safe_key    = str(self.session_key).replace(":", "_")
        cursor_file = cursors_dir / safe_key

        if cursor_file.exists():
            node_id = cursor_file.read_text(encoding="utf-8").strip()
            # Verify the node still exists in the DB
            if db.get_node(node_id) is not None:
                logger.info("[%s] resumed from cursor %s", self.session_key, node_id)
                return db, node_id
            else:
                logger.warning(
                    "[%s] cursor %s not found in DB — creating fresh node",
                    self.session_key, node_id,
                )

        # Fresh start: attach to global root
        root   = db.get_root()
        node   = db.add_node(parent_id=root.id, role="system", content=f"session:{self.session_key}")
        cursor_file.write_text(node.id, encoding="utf-8")
        logger.info("[%s] created session node %s", self.session_key, node.id)
        return db, node.id

    def _save_cursor(self) -> None:
        """Persist the current tail_node_id to the cursor file."""
        if self._tail_node_id is None:
            return
        workspace   = Path(self.config.workspace.path).expanduser().resolve()
        cursors_dir = workspace / "cursors"
        cursors_dir.mkdir(parents=True, exist_ok=True)
        safe_key    = str(self.session_key).replace(":", "_")
        cursor_file = cursors_dir / safe_key
        try:
            cursor_file.write_text(self._tail_node_id, encoding="utf-8")
        except Exception as exc:
            logger.warning("[%s] failed to save cursor: %s", self.session_key, exc)

    def _on_context_tail_advance(self) -> None:
        """
        Called by Context.add() each time the tail node advances.
        Keeps _tail_node_id and the on-disk cursor file in sync immediately,
        so a second AgentLoop for the same session always resumes from the
        latest node even if run() never completed.
        """
        self._tail_node_id = self.context.tail_node_id
        self._save_cursor()

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
    # Module loader
    # ------------------------------------------------------------------

    def _load_modules(self) -> None:
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
        """
        self._turn_count += 1
        logger.debug("[%s] turn %d (synthetic=%s)", self.session_key, self._turn_count, msg is None)

        if msg is not None:
            trace_id = msg.trace_id
            msg_id   = msg.message_id
        else:
            trace_id = str(uuid.uuid4())
            msg_id   = "synthetic"

        ev = dict(
            session_key=self.session_key,
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
                logger.info("[%s] aborted before cycle %d", self.session_key, cycle)
                yield AgentError(message="[generation aborted]", **ev)
                return

            # Stage 2: Context Assembly
            await self.context.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
            tools    = self.tool_handler.get_tool_definitions() or None
            messages = self.context.assemble(tools=tools)

            # Token budget telemetry
            tokens_used = self.context.state.get("tokens_used", 0)
            token_limit = self.config.context
            token_pct   = tokens_used / token_limit if token_limit else 0
            if token_pct >= 0.95:
                logger.warning(
                    "[%s] context at %.0f%% of token budget (%d/%d) — consider compaction",
                    self.session_key, token_pct * 100, tokens_used, token_limit,
                )
            elif token_pct >= 0.80:
                logger.info(
                    "[%s] context at %.0f%% of token budget (%d/%d)",
                    self.session_key, token_pct * 100, tokens_used, token_limit,
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
                        logger.info("[%s] aborted mid-stream", self.session_key)
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
                            "[%s] inference succeeded on fallback model '%s'",
                            self.session_key, model_name,
                        )
                    break

                fo = self.config.llm.fallback_on
                should_fallback = fo.any_error or (
                    last_http_status is not None and last_http_status in fo.http_codes
                )
                if should_fallback and model_name != model_chain[-1]:
                    logger.warning(
                        "[%s] model '%s' failed (%s) — trying next fallback",
                        self.session_key, model_name, error,
                    )
                    continue
                break

            if error:
                logger.error("[%s] LLM error (all models exhausted): %s", self.session_key, error)
                yield AgentError(message=f"[LLM error: {error}]", **ev)
                self._save_cursor()
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
            logger.debug("[%s] cycle %d — %d tool call(s)", self.session_key, cycle, len(tool_calls))
            for tc in tool_calls:
                yield AgentToolCall(call_id=tc.call_id, tool_name=tc.tool_name, args=tc.args, **ev)
                result = await self._execute_tool(tc)
                self.context.add(HistoryEntry.tool_result(result))
                yield AgentToolResult(
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    output=result.output,
                    is_error=result.is_error,
                    **ev,
                )

        else:
            logger.warning("[%s] hit max_tool_cycles (%d)", self.session_key, max_cycles)
            final_text = final_text or "[Tool cycle limit reached.]"

        # Sync cursor so the DB tail_node_id is persisted for next restart
        self._tail_node_id = self.context.tail_node_id
        self._save_cursor()

        yield AgentTextFinal(text=final_text if not streaming_active else "", **ev)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, call: ToolCall) -> ToolResult:
        proxy = {
            "function": {"name": call.tool_name, "arguments": call.args},
            "id": call.call_id,
        }
        result = await self.tool_handler.execute_tool_call(proxy)
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            output=str(result.get("result", result.get("error", "[no output]"))),
            is_error=not result.get("success", False),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        Clear in-memory context. Does NOT touch agent.db — the tree is
        permanent. The cursor stays at the current tail; the agent will
        continue to see prior history when it next assembles context.

        To start a genuinely fresh branch, advance the cursor to a new
        child node (that is Phase 2 bridge work).
        """
        self.context.clear()
        self._turn_count = 0
        logger.info("[%s] reset (in-memory only — tree intact)", self.session_key)

"""
agent.py — The 6-stage agent execution loop.
One instance per session, owned by its Lane.
Yields OutboundReply chunks; never calls the gateway directly.

Stages:
  1. Intake           — add user message to Context
  2. Context Assembly — await async hooks, then build message list via Context.assemble()
  3. Inference        — stream LLM, collect text + tool calls
  4. Tool Execution   — dispatch ToolCalls via tool_handler
  5. Result Backfill  — inject ToolResults back into Context
  6. Streaming Reply  — yield OutboundReply chunks to Lane

Streaming behaviour:
  Tool-call cycles (cycles 0..N-1) buffer text as before — we can't stream
  text that may be followed by a tool call mid-response.
  The final cycle (no tool calls returned) streams TextDelta events directly
  to the Lane as is_partial=True chunks, with a closing is_partial=False
  chunk at the end. This gives the CLI (and future bridges) word-by-word
  output with no extra latency.

Model pool:
  All named models from config.models are pre-instantiated into self._models.
  Inference walks primary → fallback list, triggering fallback according to
  config.llm.fallback_on (any_error or specific http_codes).
  Modules call agent.get_model(name) to get a named LLM instance for their
  own use (e.g. compaction with a cheaper model).

Module loading:
  On init, scans modules/ directory for packages exposing register(agent).
  Each module receives self and wires in whatever it needs — tools, prompt
  providers, context hooks. No hardcoding of module names anywhere.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

from contracts import InboundMessage, OutboundReply, ToolCall, ToolResult, SessionKey
from context import Context, HistoryEntry, HOOK_PRE_ASSEMBLE_ASYNC
from config import Config, ModelConfig
from ai import LLM, TextDelta, ToolCallAssembled, LLMError
from utils.tool_handler import ToolCallHandler

logger = logging.getLogger(__name__)

MODULES_DIR = Path("modules")


def _build_llm(cfg: ModelConfig) -> LLM:
    api_key = cfg.api_key if cfg.api_key_env.upper() != "N/A" else "no-key"
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
    )


class AgentLoop:
    """
    Owns one session's Context, tool_handler, and LLM pool.
    Called by Lane once per inbound message.
    Yields OutboundReply chunks that the Lane forwards to the gateway.
    """

    def __init__(self, session_key: SessionKey, config: Config) -> None:
        self.session_key      = session_key
        self.config           = config
        self.context          = Context(token_limit=config.context)
        self.tool_handler     = ToolCallHandler()
        self._turn_count      = 0
        self.gateway          = None  # set by Lane.__post_init__ after construction

        self._session_version = self._load_latest_version()
        self._restore_history()

        # Build the full model pool from config.models.
        # Embedding models (kind='embedding') are skipped — they're not LLM instances.
        self._models: dict[str, LLM] = {
            name: _build_llm(mc)
            for name, mc in config.models.items()
            if not mc.is_embedding
        }

        self._load_modules()

    # ------------------------------------------------------------------
    # Public model accessor (for modules)
    # ------------------------------------------------------------------

    def get_model(self, name: str) -> LLM:
        """
        Return a named LLM instance from the pool.
        Falls back to the primary model if name is not in the pool.
        Modules use this to request a specific model (e.g. cheap/fast for
        compaction or memory flush) without knowing API keys or base_urls.
        """
        if name in self._models:
            return self._models[name]
        primary = self.config.llm.primary
        logger.warning(
            "get_model('%s') — not found, falling back to primary '%s'",
            name, primary,
        )
        return self._models[primary]

    # ------------------------------------------------------------------
    # Module loader
    # ------------------------------------------------------------------

    def _load_modules(self) -> None:
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
            # Run async pre-assemble hooks first (e.g. memory indexing + embedding),
            # then call the sync assemble() pipeline.
            await self.context.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
            tools    = self.tool_handler.get_tool_definitions() or None
            messages = self.context.assemble(tools=tools)

            # Stage 3: Inference — walk primary → fallback chain
            text_chunks: list[str]      = []
            tool_calls:  list[ToolCall] = []
            error:       str | None     = None

            streamed_text: list[str] = []
            streaming_active = False

            model_chain = [self.config.llm.primary] + list(self.config.llm.fallback)

            for model_name in model_chain:
                llm = self._models[model_name]
                text_chunks    = []
                tool_calls     = []
                error          = None
                streamed_text  = []
                streaming_active = False
                last_http_status: int | None = None

                async for event in llm.stream(messages, tools=tools):
                    if isinstance(event, TextDelta):
                        text_chunks.append(event.text)

                        if not tool_calls:
                            streamed_text.append(event.text)
                            streaming_active = True
                            yield OutboundReply(
                                session_key=self.session_key,
                                text=event.text,
                                reply_to_message_id=msg.message_id,
                                trace_id=msg.trace_id,
                                is_partial=True,
                            )

                    elif isinstance(event, ToolCallAssembled):
                        tool_calls.append(ToolCall(
                            call_id=event.call_id,
                            tool_name=event.tool_name,
                            args=event.args,
                        ))

                    elif isinstance(event, LLMError):
                        error = event.message
                        if event.message.startswith("HTTP "):
                            try:
                                last_http_status = int(event.message.split()[1].rstrip(":"))
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
                    last_http_status is not None
                    and last_http_status in fo.http_codes
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

            logger.debug("[%s] cycle %d — %d tool call(s)", self.session_key, cycle, len(tool_calls))
            for tc in tool_calls:
                result = await self._execute_tool(tc)
                self.context.add(HistoryEntry.tool_result(result))

        else:
            logger.warning("[%s] hit max_tool_cycles (%d)", self.session_key, max_cycles)
            final_text = final_text or "[Tool cycle limit reached.]"

        if streaming_active and not error:
            yield OutboundReply(
                session_key=self.session_key,
                text="",
                reply_to_message_id=msg.message_id,
                trace_id=msg.trace_id,
                is_partial=False,
            )
        else:
            yield OutboundReply(
                session_key=self.session_key,
                text=final_text,
                reply_to_message_id=msg.message_id,
                trace_id=msg.trace_id,
                is_partial=False,
            )

        await self._flush_history()

    # ------------------------------------------------------------------
    # Stage 4: Tool execution
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
        self.context.clear()
        self._turn_count = 0
        self._session_version += 1
        logger.info("[%s] context reset (now v%d)", self.session_key, self._session_version)

    def _load_latest_version(self) -> int:
        safe_key = str(self.session_key).replace(":", "_")
        sessions_dir = Path("sessions") / safe_key
        if not sessions_dir.exists():
            return 1
        existing = [
            int(p.stem) for p in sessions_dir.glob("*.json") if p.stem.isdigit()
        ]
        return max(existing, default=1)

    def _restore_history(self) -> None:
        safe_key = str(self.session_key).replace(":", "_")
        path = Path("sessions") / safe_key / f"{self._session_version}.json"

        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._turn_count = data.get("turn", 0)
            for raw in data.get("dialogue", []):
                entry = HistoryEntry(
                    role=raw["role"],
                    content=raw.get("content", ""),
                    id=raw.get("id", ""),
                    index=raw.get("index", 0),
                    tool_calls=raw.get("tool_calls") or [],
                    tool_call_id=raw.get("tool_call_id"),
                )
                self.context.dialogue.append(entry)
            logger.info(
                "[%s] restored %d dialogue entries from v%d",
                self.session_key, len(self.context.dialogue), self._session_version,
            )
        except Exception as exc:
            logger.warning("[%s] _restore_history failed: %s", self.session_key, exc)

    async def _flush_history(self) -> None:
        safe_key = str(self.session_key).replace(":", "_")
        sessions_dir = Path("sessions") / safe_key
        sessions_dir.mkdir(parents=True, exist_ok=True)

        path = sessions_dir / f"{self._session_version}.json"

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
            "version":     self._session_version,
            "turn":        self._turn_count,
            "saved_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dialogue":    dialogue_raw,
        }

        try:
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug("[%s] flushed v%d -> %s", self.session_key, self._session_version, path)
        except Exception as exc:
            logger.warning("[%s] _flush_history failed: %s", self.session_key, exc)

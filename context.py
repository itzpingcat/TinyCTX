"""
context.py — Conversation history types and context assembly pipeline.
Imports only from contracts.py and stdlib. Never imports from gateway or agent.

The Context class owns:
  - HistoryEntry list (the raw dialogue)
  - Prompt provider registry (SOUL.md, AGENTS.md, memory results, etc.)
  - Four-stage hook pipeline (filter, transform, compress, post-process)
  - assemble() — produces a list[dict] ready to send to the LLM API

Modules (compression, dedup, RAG, etc.) are registered externally at startup.
Context itself never loads modules — that is main.py's concern.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from contracts import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLE_USER      = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL      = "tool"
ROLE_SYSTEM    = "system"

# ---------------------------------------------------------------------------
# Hook stages
# ---------------------------------------------------------------------------

HOOK_PRE_ASSEMBLE       = "pre_assemble"        # fn(ctx) -> None          — sync, runs inside assemble()
HOOK_PRE_ASSEMBLE_ASYNC = "pre_assemble_async"  # async fn(ctx) -> None    — awaited by agent BEFORE assemble()
HOOK_FILTER_TURN        = "filter_turn"          # fn(entry, age, ctx) -> bool   (False = drop)
HOOK_TRANSFORM_TURN     = "transform_turn"       # fn(entry, age, ctx) -> HistoryEntry | None
HOOK_POST_ASSEMBLE      = "post_assemble"        # fn(messages, ctx) -> list[dict] | None

# Execution order per turn:
#   agent awaits run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
#   agent calls ctx.assemble()
#     → HOOK_PRE_ASSEMBLE (sync, e.g. cache warm)
#     → HOOK_FILTER_TURN / HOOK_TRANSFORM_TURN  (per entry)
#     → HOOK_POST_ASSEMBLE (final reshape)


# ---------------------------------------------------------------------------
# HistoryEntry — typed dialogue record
# ---------------------------------------------------------------------------

@dataclass
class HistoryEntry:
    """
    One turn in the conversation. Covers all four roles.
    tool_calls is populated for assistant turns that invoked tools.
    tool_call_id is populated for tool result turns.
    """
    role:         str
    content:      str
    id:           str            = field(default_factory=lambda: str(uuid.uuid4()))
    index:        int            = 0     # position in dialogue; set by Context.add()
    tool_calls:   list[dict]     = field(default_factory=list)
    tool_call_id: str | None     = None

    @staticmethod
    def user(content: str) -> HistoryEntry:
        return HistoryEntry(role=ROLE_USER, content=content)

    @staticmethod
    def assistant(content: str = "", tool_calls: list[ToolCall] | None = None) -> HistoryEntry:
        raw_calls = []
        if tool_calls:
            raw_calls = [
                {"id": tc.call_id, "name": tc.tool_name, "arguments": tc.args}
                for tc in tool_calls
            ]
        return HistoryEntry(role=ROLE_ASSISTANT, content=content, tool_calls=raw_calls)

    @staticmethod
    def tool_result(result: ToolResult) -> HistoryEntry:
        return HistoryEntry(
            role=ROLE_TOOL,
            content=result.output,
            tool_call_id=result.call_id,
        )

    @staticmethod
    def system(content: str) -> HistoryEntry:
        return HistoryEntry(role=ROLE_SYSTEM, content=content)


# ---------------------------------------------------------------------------
# PromptSlot — metadata for a registered prompt provider
# ---------------------------------------------------------------------------

@dataclass
class PromptSlot:
    pid:      str
    role:     str  = ROLE_SYSTEM
    priority: int  = 0   # lower = injected first within its position


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class Context:
    """
    Assembles a list[dict] suitable for the LLM API from dialogue history
    and registered prompt providers, passing turns through a hook pipeline.

    Async hooks (HOOK_PRE_ASSEMBLE_ASYNC) are NOT run by assemble() — they
    must be awaited by the caller (AgentLoop) via run_async_hooks() before
    calling assemble(). This keeps assemble() synchronous and simple.

    Usage:
        ctx = Context()
        ctx.register_prompt("soul", lambda c: soul_md_contents)
        ctx.register_hook(HOOK_FILTER_TURN, my_trim_fn)
        ctx.register_hook(HOOK_PRE_ASSEMBLE_ASYNC, my_async_fn)
        ctx.add(HistoryEntry.user("hello"))
        await ctx.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
        messages = ctx.assemble()
    """

    def __init__(self, token_limit: int = 16384) -> None:
        self.dialogue: list[HistoryEntry] = []

        # pid -> (PromptSlot, provider callable)
        self._prompts: dict[str, tuple[PromptSlot, Callable[[Context], str | None]]] = {}

        # stage -> [(priority, insertion_order, fn)]
        self._hooks: dict[str, list] = defaultdict(list)
        self._hook_counter = 0

        # Arbitrary state bag for hooks/modules to share data during assembly
        self.state: dict[str, Any] = {}

        self.token_limit = token_limit

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------

    def register_hook(self, stage: str, fn: Callable, *, priority: int = 0) -> None:
        """
        Register a hook for a pipeline stage.
        Lower priority = runs first.
        For HOOK_PRE_ASSEMBLE_ASYNC, fn must be an async callable.
        """
        self._hook_counter += 1
        self._hooks[stage].append((priority, self._hook_counter, fn))
        self._hooks[stage].sort(key=lambda x: (x[0], x[1]))

    def unregister_hook(self, stage: str, fn: Callable) -> None:
        self._hooks[stage] = [e for e in self._hooks[stage] if e[2] is not fn]

    async def run_async_hooks(self, stage: str) -> None:
        """
        Await all hooks registered for an async stage in priority order.
        Exceptions are caught and logged so one failing hook doesn't block
        the rest.

        Call this from AgentLoop before assemble():
            await ctx.run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC)
            messages = ctx.assemble()
        """
        for _, _, fn in self._hooks[stage]:
            try:
                await fn(self)
            except Exception as exc:
                print(f"[context] async hook '{fn.__name__}' raised: {exc}")

    # ------------------------------------------------------------------
    # Prompt provider registration
    # ------------------------------------------------------------------

    def register_prompt(
        self,
        pid: str,
        provider: Callable[[Context], str | None],
        *,
        role: str = ROLE_SYSTEM,
        priority: int = 0,
    ) -> None:
        self._prompts[pid] = (PromptSlot(pid=pid, role=role, priority=priority), provider)

    def unregister_prompt(self, pid: str) -> None:
        self._prompts.pop(pid, None)

    # ------------------------------------------------------------------
    # Dialogue mutation
    # ------------------------------------------------------------------

    def add(self, entry: HistoryEntry) -> HistoryEntry:
        entry.index = len(self.dialogue)
        self.dialogue.append(entry)
        return entry

    def clear(self) -> None:
        self.dialogue.clear()
        self.state.clear()

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def _count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        tool_chars = len(json.dumps(tools)) if tools else 0
        return (sum(
            len(str(m.get("content", ""))) +
            len(json.dumps(m.get("tool_calls", [])))
            for m in messages
        ) + tool_chars) // 4

    # ------------------------------------------------------------------
    # Assembly (sync)
    # ------------------------------------------------------------------

    def assemble(self, tools: list[dict] | None = None) -> list[dict]:
        """
        Run the sync pipeline and return API-ready messages.
        Async hooks must have been awaited via run_async_hooks() beforehand.

        Stage order (sync):
          1. pre_assemble   — hooks may mutate self.dialogue or warm caches
          2. filter_turn    — drop turns
          3. transform_turn — replace/summarise turns
          4. post_assemble  — reshape final message list
        """
        n = len(self.dialogue)

        # 1. pre_assemble (sync)
        for _, _, fn in self._hooks[HOOK_PRE_ASSEMBLE]:
            fn(self)

        # Resolve prompt providers
        resolved: list[tuple[PromptSlot, str]] = []
        for slot, provider in sorted(
            self._prompts.values(), key=lambda x: x[0].priority
        ):
            try:
                content = provider(self)
            except Exception as exc:
                content = None
                print(f"[context] prompt provider '{slot.pid}' raised: {exc}")
            if content is not None:
                resolved.append((slot, content))

        # Build system block
        messages: list[dict] = []
        system_lines = [c for s, c in resolved if s.role == ROLE_SYSTEM]
        if system_lines:
            messages.append({"role": ROLE_SYSTEM, "content": "\n\n".join(system_lines)})
        for slot, content in resolved:
            if slot.role != ROLE_SYSTEM:
                messages.append({"role": slot.role, "content": content})

        # 2 & 3. filter + transform per dialogue entry
        for entry in self.dialogue:
            age = n - 1 - entry.index

            drop = False
            for _, _, fn in self._hooks[HOOK_FILTER_TURN]:
                if fn(entry, age, self) is False:
                    drop = True
                    break
            if drop:
                continue

            for _, _, fn in self._hooks[HOOK_TRANSFORM_TURN]:
                result = fn(entry, age, self)
                if result is not None:
                    entry = result

            messages.append(self._render(entry))

        # 4. post_assemble
        for _, _, fn in self._hooks[HOOK_POST_ASSEMBLE]:
            result = fn(messages, self)
            if result is not None:
                messages = result

        # Merge adjacent same-role non-tool messages
        merged: list[dict] = []
        for m in messages:
            if (
                merged
                and m["role"] == merged[-1]["role"]
                and m["role"] in (ROLE_USER, ROLE_ASSISTANT)
                and not m.get("tool_calls")
                and not merged[-1].get("tool_calls")
            ):
                merged[-1]["content"] = (
                    merged[-1]["content"] + "\n\n" + m["content"]
                ).strip()
            else:
                merged.append(dict(m))

        # Token budget enforcement — drop oldest non-system messages first.
        # Tool call + result pairs are dropped together to keep API validity.
        self.state["tokens_used"] = self._count_tokens(merged, tools)

        while self.state["tokens_used"] > self.token_limit:
            drop_idx = next(
                (i for i, m in enumerate(merged) if m["role"] != ROLE_SYSTEM),
                None,
            )
            if drop_idx is None:
                break

            if merged[drop_idx].get("tool_calls"):
                call_ids = {tc["id"] for tc in merged[drop_idx]["tool_calls"]}
                merged.pop(drop_idx)
                while drop_idx < len(merged) and merged[drop_idx]["role"] == ROLE_TOOL and merged[drop_idx].get("tool_call_id") in call_ids:
                    merged.pop(drop_idx)
            else:
                merged.pop(drop_idx)

            self.state["tokens_used"] = self._count_tokens(merged, tools)

        return merged

    def _render(self, entry: HistoryEntry) -> dict:
        if entry.role == ROLE_TOOL:
            return {
                "role":         ROLE_TOOL,
                "content":      entry.content,
                "tool_call_id": entry.tool_call_id,
            }
        if entry.role == ROLE_ASSISTANT:
            msg: dict = {"role": ROLE_ASSISTANT, "content": entry.content}
            if entry.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id":   tc["id"],
                        "type": "function",
                        "function": {
                            "name":      tc["name"],
                            "arguments": tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in entry.tool_calls
                ]
            return msg
        return {"role": entry.role, "content": entry.content}

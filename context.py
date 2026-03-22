"""
context.py — Conversation history types and context assembly pipeline.
Imports only from contracts.py and stdlib. Never imports from gateway or agent.

The Context class owns:
  - HistoryEntry list (the raw dialogue)
  - Prompt provider registry (SOUL.md, AGENTS.md, memory results, etc.)
  - Four-stage hook pipeline (filter, transform, compress, post-process)
  - assemble() — produces a list[dict] ready to send to the LLM API

Dialogue mutation:
  - add(entry)                  — append a new entry
  - edit(entry_id, new_content) — replace content in-place; no cascade
  - delete(entry_id)            — smart-delete: removes entry + dependents
                                  (tool calls cascade to their results and
                                   vice versa); re-indexes after removal
  - strip_tool_calls(entry_id)  — remove tool_calls from an assistant entry
                                  and drop its tool results, preserving the
                                  assistant's text content
  - clear()                     — wipe entire dialogue

Modules (compression, dedup, RAG, etc.) are registered externally at startup.
Context itself never loads modules — that is main.py's concern.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import logging

from contracts import ToolCall, ToolResult

logger = logging.getLogger(__name__)


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

    content may be:
      str        — plain text (the common case)
      list[dict] — OpenAI-compat content block list, used when the user
                   message includes image or file attachments.
                   Blocks follow the format:
                     {"type": "text", "text": "..."}
                     {"type": "image_url", "image_url": {"url": "data:..."}}
                   Only user-role entries ever carry a list; assistant / tool
                   / system entries always use plain strings.
    """
    role:         str
    content:      str | list     # str for most roles; list[dict] for user+attachments
    id:           str            = field(default_factory=lambda: str(uuid.uuid4()))
    index:        int            = 0     # position in dialogue; set by Context.add()
    tool_calls:   list[dict]     = field(default_factory=list)
    tool_call_id: str | None     = None

    @staticmethod
    def user(content: str | list) -> HistoryEntry:
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
                logger.exception("Async hook '%s' raised", fn.__name__)

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

    def edit(self, entry_id: str, new_content: str) -> bool:
        """
        Replace the content of a dialogue entry in-place.
        Returns True if the entry was found and updated, False otherwise.
        Does not cascade — editing an assistant turn's content does not
        touch its tool_calls or the downstream tool results.
        """
        for entry in self.dialogue:
            if entry.id == entry_id:
                entry.content = new_content
                return True
        return False

    def delete(self, entry_id: str) -> list[str]:
        """
        Remove an entry and all entries that depend on it, then re-index.

        Dependency rules (mirrors the token-budget trimming in assemble()):
          - Assistant turn with tool_calls  → also delete every tool-result
            entry whose tool_call_id is in that call set.
          - Tool-result entry               → also delete the assistant turn
            that owns the call, plus all *other* tool results belonging to
            that same assistant turn (a partial tool block is invalid).
          - User / plain assistant turn     → no dependents.

        Returns the list of entry ids that were actually removed.
        """
        ids_to_remove = self._dependents(entry_id)
        if not ids_to_remove:
            return []
        self.dialogue = [e for e in self.dialogue if e.id not in ids_to_remove]
        self._reindex()
        return list(ids_to_remove)

    def _dependents(self, entry_id: str) -> set[str]:
        """
        Return the set of entry ids that must be removed together with
        entry_id (including entry_id itself). Empty set = entry not found.
        """
        # Build lookup maps once
        by_id: dict[str, HistoryEntry] = {e.id: e for e in self.dialogue}
        if entry_id not in by_id:
            return set()

        target = by_id[entry_id]
        group: set[str] = {entry_id}

        if target.role == ROLE_ASSISTANT and target.tool_calls:
            # Delete all tool results that belong to this assistant turn
            call_ids = {tc["id"] for tc in target.tool_calls}
            for e in self.dialogue:
                if e.role == ROLE_TOOL and e.tool_call_id in call_ids:
                    group.add(e.id)

        elif target.role == ROLE_TOOL and target.tool_call_id:
            # Find the assistant turn that owns this call
            for e in self.dialogue:
                if e.role == ROLE_ASSISTANT and e.tool_calls:
                    call_ids = {tc["id"] for tc in e.tool_calls}
                    if target.tool_call_id in call_ids:
                        # Delete the whole assistant turn + all its tool results
                        group.add(e.id)
                        for r in self.dialogue:
                            if r.role == ROLE_TOOL and r.tool_call_id in call_ids:
                                group.add(r.id)
                        break

        return group

    def strip_tool_calls(self, entry_id: str) -> list[str]:
        """
        Remove the tool_calls field from an assistant entry and delete all
        downstream tool-result entries, while preserving the assistant's
        text content.

        Use this instead of delete() when the assistant turn has meaningful
        text content that should survive context trimming — mirroring the
        behaviour of the token-budget trimmer in assemble().

        Returns the list of tool-result entry ids that were removed.
        If the entry has no tool_calls, or is not found, returns [].
        """
        target = next((e for e in self.dialogue if e.id == entry_id), None)
        if target is None or target.role != ROLE_ASSISTANT or not target.tool_calls:
            return []

        call_ids = {tc["id"] for tc in target.tool_calls}
        target.tool_calls = []

        removed: list[str] = []
        kept: list[HistoryEntry] = []
        for e in self.dialogue:
            if e.role == ROLE_TOOL and e.tool_call_id in call_ids:
                removed.append(e.id)
            else:
                kept.append(e)
        self.dialogue = kept
        self._reindex()
        return removed

    def _reindex(self) -> None:
        """Reassign .index on every entry to match its current position."""
        for i, entry in enumerate(self.dialogue):
            entry.index = i

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def _count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        tool_chars = len(json.dumps(tools)) if tools else 0
        def _content_len(c) -> int:
            if isinstance(c, list):
                return sum(len(json.dumps(b)) for b in c)
            return len(str(c or ""))
        return (sum(
            _content_len(m.get("content", "")) +
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
                logger.exception("Prompt provider '%s' raised", slot.pid)
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

        # Merge adjacent same-role non-tool messages.
        # Only merge when both sides have plain-string content — never merge
        # content-block lists (they may contain images that must stay distinct).
        merged: list[dict] = []
        for m in messages:
            prev = merged[-1] if merged else None
            can_merge = (
                prev is not None
                and m["role"] == prev["role"]
                and m["role"] in (ROLE_USER, ROLE_ASSISTANT)
                and not m.get("tool_calls")
                and not prev.get("tool_calls")
                and isinstance(m.get("content"), str)
                and isinstance(prev.get("content"), str)
            )
            if can_merge:
                prev["content"] = (prev["content"] + "\n\n" + m["content"]).strip()
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

            # Drop the entry and any tool results that depend on it.
            # If the assistant turn has text content AND tool calls, preserve
            # the text — strip only the tool_calls field and the downstream
            # tool results. If there is no text content, drop the whole turn.
            if merged[drop_idx].get("tool_calls"):
                call_ids = {tc["id"] for tc in merged[drop_idx]["tool_calls"]}
                if merged[drop_idx].get("content", "").strip():
                    # Keep the turn but remove the tool_calls field so the
                    # assistant text survives in context.
                    merged[drop_idx] = {
                        k: v for k, v in merged[drop_idx].items()
                        if k != "tool_calls"
                    }
                else:
                    merged.pop(drop_idx)
                # Either way, drop the orphaned tool results.
                i = drop_idx
                while (
                    i < len(merged)
                    and merged[i]["role"] == ROLE_TOOL
                    and merged[i].get("tool_call_id") in call_ids
                ):
                    merged.pop(i)
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

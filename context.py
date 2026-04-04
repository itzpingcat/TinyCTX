"""
context.py — Conversation history types and context assembly pipeline.
Imports only from contracts.py, db.py, and stdlib. Never imports from gateway or agent.

The Context class owns:
  - Dialogue history (backed by ConversationDB when _db + _tail_node_id are set)
  - Prompt provider registry (SOUL.md, AGENTS.md, memory results, etc.)
  - Four-stage hook pipeline (filter, transform, compress, post-process)
  - assemble() — produces a list[dict] ready to send to the LLM API

Tree refactor (Phase 1)
-----------------------
When a ConversationDB is injected via set_db() and a tail node is set via
set_tail(), assemble() loads history by walking the ancestor chain from the
DB instead of reading self.dialogue. Writes via add() write immediately to
the DB and advance _tail_node_id.

When _db is None (old code path, tests), behaviour is unchanged — self.dialogue
is the source of truth and no DB writes happen. This makes the refactor
incrementally testable.

Dialogue mutation:
  - add(entry)                  — append a new entry (writes to DB if wired)
  - edit(entry_id, new_content) — replace content in-place; no cascade
  - delete(entry_id)            — smart-delete: removes entry + dependents
  - strip_tool_calls(entry_id)  — remove tool_calls from an assistant entry
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

    parent_id is the DB node_id of this entry's parent. None for entries
    that predate the tree refactor or were created without a DB wired.
    """
    role:         str
    content:      str | list     # str for most roles; list[dict] for user+attachments
    id:           str            = field(default_factory=lambda: str(uuid.uuid4()))
    index:        int            = 0     # position in dialogue; set by Context.add()
    tool_calls:   list[dict]     = field(default_factory=list)
    tool_call_id: str | None     = None
    author_id:    str | None     = None  # set for group chat user turns; None for DM / assistant / tool / system
    parent_id:    str | None     = None  # tree refactor: DB node_id of parent node

    @staticmethod
    def user(content: str | list, author_id: str | None = None) -> HistoryEntry:
        return HistoryEntry(role=ROLE_USER, content=content, author_id=author_id)

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

    Tree refactor:
      Call set_db(db) and set_tail(node_id) to switch Context into DB-backed
      mode. In this mode:
        - add() writes each entry to the DB immediately and advances _tail_node_id
        - assemble() loads history by walking DB ancestors from _tail_node_id
        - self.dialogue is kept in sync for hooks/modules that iterate it
      Without a DB wired, old in-memory behaviour is preserved.
    """

    def __init__(self, token_limit: int = 16384, image_tokens_per_block: int = 280) -> None:
        self.dialogue: list[HistoryEntry] = []

        # pid -> (PromptSlot, provider callable)
        self._prompts: dict[str, tuple[PromptSlot, Callable[[Context], str | None]]] = {}

        # stage -> [(priority, insertion_order, fn)]
        self._hooks: dict[str, list] = defaultdict(list)
        self._hook_counter = 0

        # Arbitrary state bag for hooks/modules to share data during assembly
        self.state: dict[str, Any] = {}

        self.token_limit = token_limit

        # Flat token cost charged per image_url content block when estimating
        # context usage.  image_url blocks carry raw base64 data which would
        # produce wildly inflated byte counts if measured as text.  Instead we
        # charge a flat cost matching the model's actual vision-encoder overhead.
        # Sourced from ModelConfig.tokens_per_image in config.yaml.  None means
        # the model has no vision support; _count_tokens treats it as 0 (no
        # image_url blocks will appear in the message list for such models).
        self._image_tokens_per_block: int | None = image_tokens_per_block

        # Tree refactor: optional DB backing
        self._db = None            # ConversationDB | None
        self._tail_node_id: str | None = None
        self._on_tail_advance = None  # optional callback; see set_cursor_callback()

    # ------------------------------------------------------------------
    # Tree refactor wiring
    # ------------------------------------------------------------------

    def set_db(self, db) -> None:
        """
        Wire a ConversationDB into this Context. Once set, add() writes to
        the DB and assemble() reads from it. Call set_tail() after this.
        """
        self._db = db

    def set_tail(self, node_id: str) -> None:
        """
        Point this Context at a branch tail. assemble() will walk ancestors
        from this node. add() will attach new nodes as children of this node
        and advance it.
        """
        self._tail_node_id = node_id

    def set_image_tokens(self, tokens_per_image: int | None) -> None:
        """
        Update the per-image token cost used by _count_tokens().
        Call this when the active model changes (e.g. fallback kicks in) so
        the budget estimator reflects the new model's vision-encoder overhead.
        None means the model has no vision support (image_url blocks cost 0).
        """
        self._image_tokens_per_block = tokens_per_image

    def set_cursor_callback(self, fn) -> None:
        """
        Register a zero-argument callable that is invoked every time add()
        advances the tail. Used by AgentLoop to keep its cursor file in sync
        with in-memory state even when run() is not called (e.g. direct
        context mutations in tests or background tasks).
        """
        self._on_tail_advance = fn

    @property
    def tail_node_id(self) -> str | None:
        return self._tail_node_id

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
        """
        Append a new entry. If a DB is wired, writes to the DB immediately
        and advances _tail_node_id. The entry's parent_id is set to the
        current tail so the node lands on the correct branch.
        """
        if self._db is not None and self._tail_node_id is not None:
            # Serialise content: list → JSON string for DB storage.
            # This applies to user messages with attachments AND tool results
            # with image blocks (both use list[dict] content).
            content_str = (
                json.dumps(entry.content, ensure_ascii=False)
                if isinstance(entry.content, list)
                else entry.content
            )
            tool_calls_str = (
                json.dumps(entry.tool_calls, ensure_ascii=False)
                if entry.tool_calls
                else None
            )
            node = self._db.add_node(
                parent_id=self._tail_node_id,
                role=entry.role,
                content=content_str,
                tool_calls=tool_calls_str,
                tool_call_id=entry.tool_call_id,
                author_id=entry.author_id,
            )
            entry.id        = node.id
            entry.parent_id = node.parent_id
            self._tail_node_id = node.id
            if self._on_tail_advance is not None:
                try:
                    self._on_tail_advance()
                except Exception:
                    pass  # cursor persistence failures must never interrupt add()

        entry.index = len(self.dialogue)
        self.dialogue.append(entry)
        return entry

    def clear(self) -> None:
        self.dialogue.clear()
        self.state.clear()
        # _tail_node_id is intentionally NOT reset here — clear() is a
        # user-initiated wipe of in-memory state. The tree in agent.db is
        # never mutated by clear(). The caller (bridge reset logic) is
        # responsible for moving the cursor if needed.

    def edit(self, entry_id: str, new_content: str) -> bool:
        """
        Replace the content of a dialogue entry in-place.
        Returns True if the entry was found and updated, False otherwise.
        Writes through to DB if wired.
        """
        for entry in self.dialogue:
            if entry.id == entry_id:
                entry.content = new_content
                if self._db is not None:
                    self._db.update_node_content(entry_id, new_content)
                return True
        return False

    def delete(self, entry_id: str) -> list[str]:
        """
        Remove an entry and all entries that depend on it, then re-index.
        Returns the list of entry ids that were actually removed.
        Deletes from DB if wired.
        """
        ids_to_remove = self._dependents(entry_id)
        if not ids_to_remove:
            return []
        self.dialogue = [e for e in self.dialogue if e.id not in ids_to_remove]
        self._reindex()
        if self._db is not None:
            for nid in ids_to_remove:
                self._db.delete_node(nid)
        return list(ids_to_remove)

    def _dependents(self, entry_id: str) -> set[str]:
        """
        Return the set of entry ids that must be removed together with
        entry_id (including entry_id itself). Empty set = entry not found.
        """
        by_id: dict[str, HistoryEntry] = {e.id: e for e in self.dialogue}
        if entry_id not in by_id:
            return set()

        target = by_id[entry_id]
        group: set[str] = {entry_id}

        if target.role == ROLE_ASSISTANT and target.tool_calls:
            call_ids = {tc["id"] for tc in target.tool_calls}
            for e in self.dialogue:
                if e.role == ROLE_TOOL and e.tool_call_id in call_ids:
                    group.add(e.id)

        elif target.role == ROLE_TOOL and target.tool_call_id:
            for e in self.dialogue:
                if e.role == ROLE_ASSISTANT and e.tool_calls:
                    call_ids = {tc["id"] for tc in e.tool_calls}
                    if target.tool_call_id in call_ids:
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
        text content. Returns the list of tool-result entry ids removed.
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
                if self._db is not None:
                    self._db.delete_node(e.id)
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
    # DB-backed history loading
    # ------------------------------------------------------------------

    def _load_from_db(self) -> list[HistoryEntry]:
        """
        Walk the ancestor chain from _tail_node_id and convert DB nodes to
        HistoryEntry objects. Returns an empty list if no DB is wired.
        """
        if self._db is None or self._tail_node_id is None:
            return []

        nodes = self._db.get_ancestors(self._tail_node_id)

        entries: list[HistoryEntry] = []
        for i, node in enumerate(nodes):
            # Deserialise content: JSON → list if it was stored as list.
            # Only user messages store list content (attachments).
            content: str | list = node.content
            if node.role == ROLE_USER and content.startswith("["):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, list):
                        content = parsed
                except (json.JSONDecodeError, ValueError):
                    pass  # leave as string

            tool_calls: list[dict] = []
            if node.tool_calls:
                try:
                    tool_calls = json.loads(node.tool_calls)
                except (json.JSONDecodeError, ValueError):
                    pass

            entry = HistoryEntry(
                role=node.role,
                content=content,
                id=node.id,
                index=i,
                tool_calls=tool_calls,
                tool_call_id=node.tool_call_id,
                author_id=node.author_id,
                parent_id=node.parent_id,
            )
            entries.append(entry)
        return entries

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def _count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        tool_chars  = len(json.dumps(tools)) if tools else 0
        img_cost    = self._image_tokens_per_block or 0  # 0 for non-vision models
        image_chars = img_cost * 4  # ×4 because the sum is divided by 4

        def _content_len(c) -> int:
            if isinstance(c, list):
                total = 0
                for b in c:
                    # image_url blocks carry raw base64 — counting those bytes as
                    # characters wildly inflates the estimate and causes the
                    # budget-trimmer to evict all prior conversation history.
                    # Charge a flat per-image cost matching the model's actual
                    # vision-encoder overhead (image_tokens_per_block in config.yaml).
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        total += image_chars
                    else:
                        total += len(json.dumps(b))
                return total
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

        When a DB is wired (_db + _tail_node_id set), history is loaded from
        the DB ancestor walk. Otherwise self.dialogue is used directly.

        Stage order (sync):
          1. pre_assemble   — hooks may mutate self.dialogue or warm caches
          2. filter_turn    — drop turns
          3. transform_turn — replace/summarise turns
          4. post_assemble  — reshape final message list
        """
        # Load from DB if wired; otherwise use in-memory dialogue.
        if self._db is not None and self._tail_node_id is not None:
            source = self._load_from_db()
            logger.debug(
                "[assemble] loaded %d entries from DB (tail=%s)",
                len(source), self._tail_node_id,
            )
            # Keep self.dialogue in sync so hooks that iterate it see current state.
            self.dialogue = source
        else:
            source = self.dialogue
            logger.debug("[assemble] using in-memory dialogue (%d entries)", len(source))

        n = len(source)

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
        for entry in source:
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

        # Token budget enforcement
        self.state["tokens_used_pre_trim"] = self._count_tokens(merged, tools)
        self.state["tokens_used"] = self.state["tokens_used_pre_trim"]
        self.state["budget_trimmed"] = False

        while self.state["tokens_used"] > self.token_limit:
            self.state["budget_trimmed"] = True
            drop_idx = next(
                (i for i, m in enumerate(merged) if m["role"] != ROLE_SYSTEM),
                None,
            )
            if drop_idx is None:
                break

            if merged[drop_idx].get("tool_calls"):
                call_ids = {tc["id"] for tc in merged[drop_idx]["tool_calls"]}
                if merged[drop_idx].get("content", "").strip():
                    merged[drop_idx] = {
                        k: v for k, v in merged[drop_idx].items()
                        if k != "tool_calls"
                    }
                else:
                    merged.pop(drop_idx)
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
                "content":      entry.content,  # str or list[dict] for image blocks
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

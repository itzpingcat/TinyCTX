"""
context.py — Conversation history types and context assembly pipeline.
Imports only from contracts.py, db.py, and stdlib. Never imports from gateway or agent.

The Context class owns:
  - Dialogue history (backed by ConversationDB)
  - Prompt provider registry (SOUL.md, AGENTS.md, memory results, etc.)
  - Four-stage hook pipeline (filter, transform, compress, post-process)
  - assemble() — produces (list[dict], AssembleMeta) ready to send to the LLM API

Constructor
-----------
Context(db, tail_node_id, token_limit, image_tokens_per_block, token_fuzz)

All required fields are supplied at construction time. db and tail_node_id
must be provided — there is no lazy/optional wiring path.

set_tail(node_id) exists solely to advance the cursor as the cycle writes
tool-call and tool-result nodes mid-turn.

assemble() returns (messages, AssembleMeta) where AssembleMeta is a small
dataclass carrying tokens_pre_trim, tokens_used, was_trimmed, and
invalidated_tags. Callers (AgentCycle) read meta fields directly — nothing
essential is side-channelled through self.state (some fields are also
mirrored into self.state for hook back-compat).

Dialogue entries (HistoryEntry) carry a .tags field and stay as HistoryEntry
objects through filter_turn, transform_turn, the adjacent-message merge, AND
the token-budget trim loop — rendering to OpenAI-format dicts happens exactly
once, as the last step before HOOK_POST_ASSEMBLE. meta.invalidated_tags is a
plain set-diff (tags seen at any point this turn minus tags still present on
a surviving entry) computed after trimming is fully resolved, letting a
module know — one turn later, via db.set_state — that content it tagged
(e.g. a loaded skill) is now gone, without inferring that from string content.

Session state is loaded by AgentCycle at construction time via
db.load_session_state(); Context does not call it. _load_state_from_db()
is removed from Context entirely. assemble() receives session state via
a call to db.load_session_state() internally for backwards compatibility
with hooks that read ctx.state["session"].

Async hooks (HOOK_PRE_ASSEMBLE_ASYNC) are NOT run by assemble() — they
must be awaited by the caller (AgentCycle) via run_async_hooks() before
calling assemble(). This keeps assemble() synchronous and simple.
"""

from __future__ import annotations

import json
import tiktoken
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import logging

from TinyCTX.contracts import ToolCall, ToolResult
from TinyCTX.utils.sanitize import sanitize_brackets as _sanitize_brackets

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
#     → adjacent-message merge + token-budget trim   (still HistoryEntry — see below)
#     → render to OpenAI-format dicts
#     → HOOK_POST_ASSEMBLE (final reshape — genuinely final: runs after merge/trim/render)
#
# NOTE: dialogue entries stay as HistoryEntry (carrying .tags) all the way through
# filter_turn, transform_turn, the adjacent-message merge, AND the token-budget
# trim loop. Rendering to plain OpenAI-format dicts (_render()) happens only once,
# as the very last step before HOOK_POST_ASSEMBLE. This means a tag survives iff
# it's still present on some entry in the fully-trimmed list — no need to infer
# survival from string content, and no shadow keys needed to smuggle .tags through
# dict form. See AssembleMeta.invalidated_tags for the turn-level result.
#
# Any transform_turn hook that destroys/replaces an entry's substantive content
# (e.g. ctx_tools' trim/tokenade stubbing tool output) MUST clear entry.tags on
# the copy it returns — otherwise a tag will look like it "survived" a turn whose
# actual content is gone. Cosmetic edits (e.g. token_sanitize stripping control
# tokens, cot_strip removing <think> blocks) should leave tags alone.


# ---------------------------------------------------------------------------
# AssembleMeta — returned alongside messages from assemble()
# ---------------------------------------------------------------------------

@dataclass
class AssembleMeta:
    tokens_pre_trim:   int
    tokens_used:       int
    was_trimmed:       bool
    # Tags (see HistoryEntry.tags) that were present somewhere in the loaded
    # dialogue at the start of this assemble() call but are absent from every
    # surviving entry by the end — i.e. dropped by filter_turn, gutted by a
    # destructive transform_turn (which clears tags on its copy), or popped by
    # the token-budget trim loop. Modules that tag entries for their own
    # bookkeeping (e.g. "skill:foo" on a use_skill result) read this to learn,
    # after the fact, that the tagged content is gone this turn — then persist
    # that via db.set_state for a prompt provider to act on next turn. This is
    # diagnostic-only, computed after trimming is fully done; it must never be
    # used to decide whether to trim (that would reintroduce a circular
    # trim-depends-on-content-depends-on-trim dependency).
    invalidated_tags:  frozenset[str] = field(default_factory=frozenset)


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

    parent_id is the DB node_id of this entry's parent.

    tags is an opaque set of module-assigned labels (e.g. "skill:foo") used to
    track whether specific content survived context assembly. Tags are derived
    from structured data (the originating tool call's name/arguments), never
    from parsing content. Any hook that destroys/replaces an entry's
    substantive content must clear tags on the copy it returns — see the
    HOOK_* comments above. Tags ride along through filter_turn, transform_turn,
    the adjacent-message merge, and the token-budget trim loop; they are
    dropped when the entry is finally rendered to an OpenAI-format dict
    (they have no meaning to the LLM API).
    """
    role:         str
    content:      str | list     # str for most roles; list[dict] for user+attachments
    id:           str            = field(default_factory=lambda: str(uuid.uuid4()))
    index:        int            = 0     # position in dialogue; set by Context.add()
    tool_calls:   list[dict]     = field(default_factory=list)
    tool_call_id: str | None     = None
    author_id:    str | None     = None  # TinyCTX username of sender; None for assistant/tool/system
    parent_id:    str | None     = None  # DB node_id of parent node
    tags:         frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def user(content: str | list, author_id: str | None = None) -> HistoryEntry:
        return HistoryEntry(role=ROLE_USER, content=content, author_id=author_id)

    @staticmethod
    def assistant(content: str = "", tool_calls: list[ToolCall] | None = None, author_id: str | None = None) -> HistoryEntry:
        raw_calls = []
        if tool_calls:
            raw_calls = [
                {"id": tc.call_id, "name": tc.tool_name, "arguments": tc.args}
                for tc in tool_calls
            ]
        return HistoryEntry(role=ROLE_ASSISTANT, content=content, tool_calls=raw_calls, author_id=author_id)

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

    Constructor: Context(db, tail_node_id, token_limit, image_tokens_per_block, token_fuzz)

    All fields are required at construction time — no post-construction wiring.
    set_tail(node_id) is the only setter that exists, used mid-turn to advance
    the cursor as the cycle writes tool-call and tool-result nodes.

    assemble() returns (messages, AssembleMeta). Callers read meta directly.
    """

    def __init__(
        self,
        db,                          # ConversationDB
        tail_node_id: str,
        token_limit: int = 16384,
        image_tokens_per_block: int | None = 280,
        token_fuzz: float = 1.1,
    ) -> None:
        self._db = db
        self._tail_node_id: str = tail_node_id
        self.token_limit = token_limit
        self._image_tokens_per_block: int | None = image_tokens_per_block
        self.token_fuzz = token_fuzz

        self.dialogue: list[HistoryEntry] = []

        # pid -> (PromptSlot, provider callable)
        self._prompts: dict[str, tuple[PromptSlot, Callable[[Context], str | None]]] = {}

        # stage -> [(priority, insertion_order, fn)]
        self._hooks: dict[str, list] = defaultdict(list)
        self._hook_counter = 0

        # Arbitrary state bag for hooks/modules to share data during assembly.
        # NOTE: tokens_used, tokens_pre_trim, was_trimmed are no longer written
        # here — they are returned via AssembleMeta from assemble().
        # ctx.state["session"] IS still written by assemble() for hook compat.
        self.state: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Cursor advance (only setter that exists post-construction)
    # ------------------------------------------------------------------

    def set_tail(self, node_id: str) -> None:
        """Advance the cursor mid-turn as the cycle writes new nodes."""
        self._tail_node_id = node_id

    def set_image_tokens(self, tokens_per_image: int | None) -> None:
        """Update per-image token cost when active model changes (fallback)."""
        self._image_tokens_per_block = tokens_per_image

    @property
    def tail_node_id(self) -> str:
        return self._tail_node_id

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------

    def register_hook(self, stage: str, fn: Callable, *, priority: int = 0) -> None:
        self._hook_counter += 1
        self._hooks[stage].append((priority, self._hook_counter, fn))
        self._hooks[stage].sort(key=lambda x: (x[0], x[1]))

    def unregister_hook(self, stage: str, fn: Callable) -> None:
        self._hooks[stage] = [e for e in self._hooks[stage] if e[2] is not fn]

    async def run_async_hooks(self, stage: str) -> None:
        """Await all hooks registered for an async stage in priority order."""
        for _, _, fn in self._hooks[stage]:
            try:
                await fn(self)
            except Exception:
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
        Append a new entry. Writes to the DB immediately and advances
        _tail_node_id. entry.parent_id is set to the current tail so
        the node lands on the correct branch.
        """
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

        entry.index = len(self.dialogue)
        self.dialogue.append(entry)
        return entry

    def add_tool_result(self, result: ToolResult) -> None:
        """
        Write a tool result into the context. If the result carries image
        data, the image is stored as a content block list on the tool entry
        itself. ai.py detects this before sending and injects a synthetic
        user turn with the image_url block into the outgoing payload only.

        This keeps the image paired with its tool result for trimming: if
        the tool result is trimmed out, the image goes with it.
        """
        if result.is_image and result.image_mime and result.image_b64:
            entry = HistoryEntry(
                role=ROLE_TOOL,
                content=[
                    {"type": "text", "text": result.output},
                    {"type": "image_url", "image_url": {"url": f"data:{result.image_mime};base64,{result.image_b64}"}},
                ],
                tool_call_id=result.call_id,
            )
            self.add(entry)
        else:
            self.add(HistoryEntry.tool_result(result))

    def clear(self) -> None:
        self.dialogue.clear()
        self.state.clear()

    def edit(self, entry_id: str, new_content: str) -> bool:
        for entry in self.dialogue:
            if entry.id == entry_id:
                entry.content = new_content
                self._db.update_node_content(entry_id, new_content)
                return True
        return False

    def delete(self, entry_id: str) -> list[str]:
        ids_to_remove = self._dependents(entry_id)
        if not ids_to_remove:
            return []
        self.dialogue = [e for e in self.dialogue if e.id not in ids_to_remove]
        self._reindex()
        for nid in ids_to_remove:
            self._db.delete_node(nid)
        return list(ids_to_remove)

    def _dependents(self, entry_id: str) -> set[str]:
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
                self._db.delete_node(e.id)
            else:
                kept.append(e)
        self.dialogue = kept
        self._reindex()
        return removed

    def _reindex(self) -> None:
        for i, entry in enumerate(self.dialogue):
            entry.index = i

    # ------------------------------------------------------------------
    # DB-backed history loading
    # ------------------------------------------------------------------

    def _load_from_db(self) -> list[HistoryEntry]:
        """
        Walk the ancestor chain from _tail_node_id and convert DB nodes to
        HistoryEntry objects.

        Content is stored fully-formed by runtime.push() — plain text for
        text-only messages, JSON-serialised content block list for messages
        with inlined attachments, or text with a <files> reference note
        appended for reference-only attachments.  No rehydration is needed
        here; we just deserialise list content from JSON when present.
        """
        nodes = self._db.get_ancestors(self._tail_node_id)

        entries: list[HistoryEntry] = []
        for i, node in enumerate(nodes):
            _VALID_BLOCK_TYPES = {"text", "image_url", "image", "document"}
            content: str | list = node.content
            if node.role in (ROLE_USER, ROLE_TOOL) and isinstance(content, str) and content.startswith("["):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, list) and all(
                        isinstance(b, dict) and b.get("type") in _VALID_BLOCK_TYPES
                        for b in parsed
                    ):
                        content = parsed
                    elif isinstance(parsed, list):
                        logger.warning(
                            "[load_from_db] node %s has list content with unrecognised "
                            "block types — treating as plain string to avoid API errors",
                            node.id,
                        )
                except (json.JSONDecodeError, ValueError):
                    pass

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

    _tiktoken_enc: "tiktoken.Encoding | None" = None

    @classmethod
    def _get_encoder(cls) -> "tiktoken.Encoding | None":
        if cls._tiktoken_enc is None:
            try:
                cls._tiktoken_enc = tiktoken.get_encoding("o200k_base")
            except Exception:
                cls._tiktoken_enc = None
        return cls._tiktoken_enc

    def _count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        img_cost = self._image_tokens_per_block or 0
        enc      = self._get_encoder()

        def _tokenize(text: str) -> int:
            if enc is None:
                return len(text) // 4
            return len(enc.encode(text, disallowed_special=()))

        def _content_tokens(c) -> int:
            if isinstance(c, list):
                total = 0
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        total += img_cost
                    else:
                        total += _tokenize(json.dumps(b))
                return total
            return _tokenize(str(c or ""))

        tool_tokens = _tokenize(json.dumps(tools)) if tools else 0

        raw = sum(
            _content_tokens(m.get("content", "")) +
            _tokenize(json.dumps(m.get("tool_calls", [])))
            for m in messages
        ) + tool_tokens

        return int(raw * self.token_fuzz)

    # ------------------------------------------------------------------
    # Assembly (sync) — returns (messages, AssembleMeta)
    # ------------------------------------------------------------------

    def assemble(self, tools: list[dict] | None = None) -> tuple[list[dict], AssembleMeta]:
        """
        Run the sync pipeline and return (API-ready messages, AssembleMeta).
        Async hooks must have been awaited via run_async_hooks() beforehand.

        History is always loaded from the DB ancestor walk.

        Stage order (sync):
          1. pre_assemble        — hooks may mutate self.dialogue or warm caches
          2. filter_turn         — drop turns
          3. transform_turn      — replace/summarise turns
          4. adjacent-turn merge — still HistoryEntry; unions .tags on merge
          5. token-budget trim   — still HistoryEntry; pops/truncates oldest first
          6. render              — HistoryEntry -> OpenAI-format dict (only now)
          7. post_assemble       — final reshape of the rendered dict list

        Entries (dialogue AND synthetic system/footer prompt entries) stay as
        HistoryEntry objects — carrying .tags — all the way through steps 2-5.
        This is what lets AssembleMeta.invalidated_tags be a plain set-diff
        instead of needing to infer survival from rendered dict content.
        """
        # Load session state and put it in ctx.state["session"] for hook compat.
        session_state, delta_depth = self._db.load_session_state(self._tail_node_id)
        self.state["session"] = session_state
        self.state["session_delta_depth"] = delta_depth
        logger.debug(
            "[assemble] session state replayed (depth=%d, keys=%s)",
            delta_depth, list(session_state.keys()),
        )

        source = self._load_from_db()
        logger.debug(
            "[assemble] loaded %d entries from DB (tail=%s)",
            len(source), self._tail_node_id,
        )
        # Keep self.dialogue in sync so hooks that iterate it see current state.
        self.dialogue = source

        n = len(source)
        # Accumulates every tag seen on ANY intermediate version of any entry
        # during the per-entry loop below — not just the pre-hook snapshot.
        # Tags are typically assigned BY a transform_turn hook (e.g. the skills
        # module tagging a use_skill result), so a tag that gets added and then
        # destructively cleared within this same pass must still count as
        # "was present this turn" or it can never show up as invalidated.
        seen_tags: set[str] = set()

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
            except Exception:
                content = None
                logger.exception("Prompt provider '%s' raised", slot.pid)
            if content is not None:
                resolved.append((slot, content))

        # Build system block as a synthetic entry (never trimmed — trim skips role=system).
        entries: list[HistoryEntry] = []
        system_lines = [c for s, c in resolved if s.role == ROLE_SYSTEM]
        if system_lines:
            entries.append(HistoryEntry(role=ROLE_SYSTEM, content="\n\n".join(system_lines)))

        # Non-system prompts (e.g. role=user footer) are deferred until after
        # dialogue history so they land on the latest user message, not the first.
        deferred_prompts = [(s, c) for s, c in resolved if s.role != ROLE_SYSTEM]

        # 2 & 3. filter + transform per dialogue entry
        for entry in source:
            age = n - 1 - entry.index
            seen_tags |= entry.tags

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
                    seen_tags |= entry.tags

            if entry.role == ROLE_USER and entry.author_id is None:
                if entry.parent_id is not None:
                    logger.error(
                        "[assemble] user entry id=%s (age=%d) has no author_id — "
                        "【prefix】 will be missing in LLM context. "
                        "Check that runtime.push() wrote author_id correctly for node %s.",
                        entry.id, age, entry.id,
                    )

            if entry.role == ROLE_USER and entry.author_id is not None:
                label = entry.author_id
                raw = entry.content
                # Fullwidth 【】 delimiters are visually distinct from ASCII []
                # and cannot be spoofed by user content after bracket sanitization.
                prefix = f"【{label}】: "  # 【label】:
                if isinstance(raw, str):
                    labelled_content: str | list = prefix + _sanitize_brackets(raw)
                else:
                    blocks = list(raw)
                    first_text: int | None = next(
                        (i for i, b in enumerate(blocks) if b.get("type") == "text"), None
                    )
                    if first_text is not None:
                        existing = blocks[first_text]
                        sanitized_text = _sanitize_brackets(existing["text"])
                        blocks[first_text] = {**existing, "text": prefix + sanitized_text}  # type: ignore[index]
                    else:
                        blocks.insert(0, {"type": "text", "text": prefix})
                    labelled_content = blocks
                entry = replace(entry, content=labelled_content)

            entries.append(entry)

        # Insert deferred non-system prompts (e.g. footer) as synthetic entries
        # BEFORE the last user entry so the merge produces: <footer>\n\n[user message].
        # Priority is respected within the deferred set.
        if deferred_prompts:
            sorted_deferred = sorted(deferred_prompts, key=lambda x: x[0].priority)
            last_user_idx = next(
                (i for i in range(len(entries) - 1, -1, -1)
                 if entries[i].role == ROLE_USER),
                None,
            )
            synthetic = [HistoryEntry(role=s.role, content=c) for s, c in sorted_deferred]
            if last_user_idx is not None:
                entries[last_user_idx:last_user_idx] = synthetic
            else:
                entries.extend(synthetic)

        # 4. Merge adjacent same-role non-tool entries (still HistoryEntry — tags union).
        merged: list[HistoryEntry] = []
        for m in entries:
            prev = merged[-1] if merged else None
            can_merge = (
                prev is not None
                and m.role == prev.role
                and m.role in (ROLE_USER, ROLE_ASSISTANT)
                and not m.tool_calls
                and not prev.tool_calls
                and isinstance(m.content, str)
                and isinstance(prev.content, str)
            )
            if can_merge:
                prev.content = (prev.content + "\n\n" + m.content).strip()
                prev.tags = prev.tags | m.tags
            else:
                merged.append(replace(m))

        # 5. Token budget enforcement (still HistoryEntry).
        tokens_pre_trim = self._count_tokens_entries(merged, tools)
        tokens_used     = tokens_pre_trim
        was_trimmed     = False

        while tokens_used > self.token_limit:
            was_trimmed = True
            drop_idx = next(
                (i for i, e in enumerate(merged) if e.role != ROLE_SYSTEM),
                None,
            )
            if drop_idx is None:
                break

            if merged[drop_idx].tool_calls:
                call_ids = {tc["id"] for tc in merged[drop_idx].tool_calls}
                if isinstance(merged[drop_idx].content, str) and merged[drop_idx].content.strip():
                    merged[drop_idx] = replace(merged[drop_idx], tool_calls=[])
                else:
                    merged.pop(drop_idx)
                i = drop_idx
                while (
                    i < len(merged)
                    and merged[i].role == ROLE_TOOL
                    and merged[i].tool_call_id in call_ids
                ):
                    merged.pop(i)
            else:
                merged.pop(drop_idx)

            tokens_used = self._count_tokens_entries(merged, tools)

        # Tag survival: any tag seen on any intermediate entry version during
        # this turn's processing but absent from every surviving entry after
        # filter/transform/merge/trim is invalidated.
        surviving_tags: set[str] = set().union(*(e.tags for e in merged)) if merged else set()
        invalidated_tags = frozenset(seen_tags - surviving_tags)

        # Write back into state BEFORE post_assemble runs, so a post_assemble
        # hook can read ctx.state["invalidated_tags"] (e.g. to persist a
        # "this tagged content is gone" note via db.set_state for next turn).
        self.state["tokens_used_pre_trim"] = tokens_pre_trim
        self.state["tokens_used"]          = tokens_used
        self.state["budget_trimmed"]       = was_trimmed
        self.state["invalidated_tags"]     = invalidated_tags
        self.state["surviving_tags"]       = frozenset(surviving_tags)

        # 6. Render HistoryEntry -> OpenAI-format dict (the one and only render pass).
        messages: list[dict] = [self._render(e) for e in merged]

        # 7. post_assemble — genuinely final now: runs after merge + trim + render.
        for _, _, fn in self._hooks[HOOK_POST_ASSEMBLE]:
            result = fn(messages, self)
            if result is not None:
                messages = result

        meta = AssembleMeta(
            tokens_pre_trim=tokens_pre_trim,
            tokens_used=tokens_used,
            was_trimmed=was_trimmed,
            invalidated_tags=invalidated_tags,
        )
        return messages, meta

    def _count_tokens_entries(self, entries: list[HistoryEntry], tools: list[dict] | None) -> int:
        """Render entries to dict form just for counting — doesn't mutate entries."""
        return self._count_tokens([self._render(e) for e in entries], tools)

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

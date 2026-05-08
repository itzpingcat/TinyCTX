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

Runtime refactor (Phase 3)
--------------------------
_load_state_from_db() walks the ancestor chain tip→root one hop at a time via
db.get_parent(), merging state_delta JSON objects as it goes. The walk stops
early when it hits a node whose state_delta contains "_checkpoint": true, or
at the root if no checkpoint exists. The merged result is written to
self.state["session"] before every assemble() call.

assemble() detects multi-author branches (more than one distinct non-None
author_id in the loaded history) and prepends "[author_name]: " to each user
turn's content before rendering. This replaces GroupLane buffer formatting.

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
import tiktoken
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import logging

from TinyCTX.contracts import ToolCall, ToolResult

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
    author_id:    str | None     = None  # stable per-platform sender id; None for DM/assistant/tool/system
    author_name:  str | None     = None  # display name at send time; None for DM/assistant/tool/system
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
                author_name=entry.author_name,
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

    def _load_state_from_db(self) -> tuple[dict, int]:
        """
        Walk ancestors tip→root one hop at a time, merging state_delta JSON
        objects to reconstruct current session state.

        Stops early when it hits a node with "_checkpoint": true in its
        state_delta (all keys guaranteed present). Walks to root otherwise.

        Returns (state_dict, depth) where depth is the number of nodes visited.
        The caller (Runtime.push) uses depth to decide whether to write a
        full checkpoint on the triggering node.

        Keys filled by earlier (tip-side) nodes win; we never overwrite a key
        once it has been set (tip→root order = most-recent wins).
        """
        if self._db is None or self._tail_node_id is None:
            return {}, 0

        state: dict = {}
        depth = 0
        node_id = self._tail_node_id

        while True:
            node = self._db.get_node(node_id)
            if node is None:
                break
            depth += 1

            if node.state_delta:
                try:
                    delta: dict = json.loads(node.state_delta)
                except (json.JSONDecodeError, ValueError):
                    delta = {}
                # Merge: only fill keys not yet seen (tip-wins).
                for k, v in delta.items():
                    if k not in state:
                        state[k] = v
                # Stop at a checkpoint — all keys present, no need to go further.
                if delta.get("_checkpoint"):
                    break

            # Stop at root (no parent).
            if node.parent_id is None:
                break
            node_id = node.parent_id

        # Remove the internal marker from the consumer-facing state dict.
        state.pop("_checkpoint", None)
        return state, depth

    def _load_from_db(self) -> list[HistoryEntry]:
        """
        Walk the ancestor chain from _tail_node_id and convert DB nodes to
        HistoryEntry objects. Returns an empty list if no DB is wired.

        For user nodes that have attachment_paths set, re-hydrates Attachment
        objects from disk and calls build_content_blocks() so the LLM sees
        the same content block list as on the original turn.
        """
        if self._db is None or self._tail_node_id is None:
            return []

        nodes = self._db.get_ancestors(self._tail_node_id)
        # Index of the last node in the list — used to skip attachment
        # re-hydration for all but the most recent user node. Re-sending
        # base64 image bytes from old history turns wastes memory and tokens;
        # only the current turn's attachments need to be in the message list.
        last_idx = len(nodes) - 1

        # Lazy import to avoid circular deps — attachments.py imports from contracts/config only.
        try:
            from TinyCTX.utils.attachments import build_content_blocks as _build_blocks, classify as _classify
            from TinyCTX.contracts import Attachment, AttachmentKind
            import mimetypes as _mimetypes
            _att_available = True
        except ImportError:
            _att_available = False

        entries: list[HistoryEntry] = []
        for i, node in enumerate(nodes):
            # Deserialise content: JSON → list if it was stored as list.
            # Only user messages store list content (attachments).
            _VALID_BLOCK_TYPES = {"text", "image_url", "image", "document"}
            content: str | list = node.content
            if node.role == ROLE_USER and content.startswith("["):
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
                    pass  # leave as string

            # Re-hydrate attachments from attachment_paths if content is still
            # a plain string (i.e. the list content wasn't stored inline).
            # Only re-hydrate for the most recent node — re-sending raw image
            # bytes from old history turns causes a compounding memory leak.
            if (
                node.role == ROLE_USER
                and isinstance(content, str)
                and node.attachment_paths
                and _att_available
                and i == last_idx
            ):
                try:
                    paths: list[str] = json.loads(node.attachment_paths)
                    atts: list = []
                    for path_str in paths:
                        from pathlib import Path as _Path
                        p = _Path(path_str)
                        if p.exists():
                            data = p.read_bytes()
                            mime = _mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                            kind = _classify(Attachment(filename=p.name, data=data, mime_type=mime))
                            atts.append(Attachment(filename=p.name, data=data, mime_type=mime, kind=kind))
                        else:
                            logger.warning("[load_from_db] attachment path missing: %s", path_str)
                    if atts and self._db is not None:
                        # We need model/att config to rebuild blocks. Try to get from state.
                        # Fall back to a best-effort text-only build if unavailable.
                        from TinyCTX.config import AttachmentConfig, ModelConfig
                        att_cfg = AttachmentConfig()
                        # Use a dummy vision-disabled model config — the original
                        # content blocks (with base64 images) are expensive to
                        # reconstruct and the text/file references are sufficient
                        # for history context. Vision content is re-sent fresh on
                        # each new turn anyway.
                        dummy_model = ModelConfig(
                            model="dummy", base_url="http://localhost",
                            vision=False, tokens_per_image=None,
                        )
                        import os as _os
                        workspace = _Path(_os.getcwd())  # best-effort; overridden by caller if possible
                        rebuilt = _build_blocks(
                            text=content,
                            attachments=tuple(atts),
                            model_cfg=dummy_model,
                            att_cfg=att_cfg,
                            workspace=workspace,
                        )
                        content = rebuilt
                except Exception:
                    logger.exception("[load_from_db] failed to re-hydrate attachments for node %s", node.id)

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
                author_name=node.author_name,
                parent_id=node.parent_id,
            )
            entries.append(entry)
        return entries

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    # Lazy-loaded tiktoken encoder. o200k_base is a close enough tokenizer
    # for most open-weight models served via llama.cpp (Llama, Mistral, Gemma,
    # DeepSeek, Qwen, etc.). It won't be exact for every model but is far more
    # accurate than the old chars//4 heuristic.
    _tiktoken_enc: "tiktoken.Encoding | None" = None

    @classmethod
    def _get_encoder(cls) -> "tiktoken.Encoding":
        if cls._tiktoken_enc is None:
            try:
                cls._tiktoken_enc = tiktoken.get_encoding("o200k_base")
            except Exception:
                cls._tiktoken_enc = None  # fall back to heuristic on failure
        return cls._tiktoken_enc

    def _count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        img_cost = self._image_tokens_per_block or 0  # 0 for non-vision models
        enc      = self._get_encoder()

        def _tokenize(text: str) -> int:
            if enc is None:
                return len(text) // 4
            return len(enc.encode(text, disallowed_special=()))

        def _content_tokens(c) -> int:
            if isinstance(c, list):
                total = 0
                for b in c:
                    # image_url blocks carry raw base64 — tokenizing those bytes
                    # would wildly inflate the count. Charge the flat per-image
                    # cost from ModelConfig.tokens_per_image instead.
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

        # 1.05x fudge factor: tiktoken uses cl100k_base which won't match the
        # model's actual tokenizer exactly. A 5% overhead means the trimmer
        # fires slightly early rather than slightly late.
        return int(raw * 1.05)

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
            # Reconstruct session state from delta chain before loading dialogue.
            # This is independent of token-budget trimming — state is always
            # fully replayed regardless of how many dialogue turns get dropped.
            session_state, delta_depth = self._load_state_from_db()
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

        # Detect multi-author branches: if more than one distinct non-None
        # author_id appears in the loaded history, prepend "[Name]: " to every
        # user turn so the LLM can distinguish speakers. This replaces the old
        # GroupLane buffer formatting without any in-memory buffering.
        distinct_authors = {
            e.author_id for e in source
            if e.role == ROLE_USER and e.author_id is not None
        }
        is_multi_author = len(distinct_authors) > 1

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

            # Multi-author formatting: prepend "[Name]: " to user turns.
            # We work on a shallow copy so the original HistoryEntry is not mutated.
            if is_multi_author and entry.role == ROLE_USER:
                label = entry.author_name or entry.author_id or "unknown"
                raw = entry.content
                if isinstance(raw, str):
                    labelled_content: str | list = f"[{label}]: {raw}"
                else:
                    # list[dict] content (attachments): prepend label to first text block
                    # or insert a new text block at position 0.
                    blocks = list(raw)
                    first_text = next(
                        (i for i, b in enumerate(blocks) if b.get("type") == "text"), None
                    )
                    if first_text is not None:
                        blocks[first_text] = {
                            **blocks[first_text],
                            "text": f"[{label}]: {blocks[first_text]['text']}",
                        }
                    else:
                        blocks.insert(0, {"type": "text", "text": f"[{label}]: "})
                    labelled_content = blocks
                from dataclasses import replace
                entry = replace(entry, content=labelled_content)

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
            content = entry.content
            if isinstance(content, list):
                # Tool result content must be a plain string for API compatibility.
                # Flatten list content (e.g. image blocks) to a JSON string.
                content = json.dumps(content, ensure_ascii=False)
            return {
                "role":         ROLE_TOOL,
                "content":      content,
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

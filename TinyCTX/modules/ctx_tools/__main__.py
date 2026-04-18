from __future__ import annotations
import json
import re


def register(agent, config=None):
    if config is None:
        try:
            from TinyCTX.modules.ctx_tools import EXTENSION_META
            config = EXTENSION_META.get("default_config", {})
        except ImportError:
            config = {}
    _register_dedup(agent.context, config)
    _register_cot_strip(agent.context, config)
    _register_trim(agent.context, config)
    _register_tokenade(agent.context, config)


def _register_dedup(context, config):
    dedup_after = config.get("same_call_dedup_after", 3)

    # Maps suppressed entry index -> True (for tool result turns)
    suppressed_tool:  set[int] = set()
    # Maps suppressed tool_call id -> True (for assistant tool_calls list)
    suppressed_calls: set[str] = set()

    def pre_assemble(ctx):
        suppressed_tool.clear()
        suppressed_calls.clear()

        dialogue = ctx.dialogue
        n = len(dialogue)

        # Build call_map from ALL assistant turns unconditionally.
        # Keyed by tool_call id -> tool_call dict.
        call_map: dict[str, dict] = {}
        for entry in dialogue:
            for tc in entry.tool_calls:
                call_map[tc["id"]] = tc

        # Walk newest-to-oldest. First time we see a sig = canonical (keep).
        # Subsequent occurrences beyond dedup_after turns = suppress.
        # "age" here is the number of tool-result turns seen with the same sig
        # before this one, i.e. how many newer copies already exist.
        sig_last_seen: dict[str, int] = {}  # sig -> index of most recent occurrence

        for i in reversed(range(n)):
            entry = dialogue[i]
            if entry.role != "tool" or not entry.tool_call_id:
                continue
            tc = call_map.get(entry.tool_call_id)
            if not tc:
                # Orphaned tool result — its paired assistant call is missing.
                # Suppress it so the model never sees a dangling result.
                suppressed_tool.add(i)
                continue
            sig = tc["name"] + "::" + json.dumps(tc["arguments"], sort_keys=True)
            if sig in sig_last_seen:
                # A newer copy already exists; suppress this older one if it's
                # far enough back (distance measured in dialogue entries).
                distance = sig_last_seen[sig] - i
                if distance > dedup_after:
                    suppressed_tool.add(i)
                    suppressed_calls.add(tc["id"])
                    continue  # don't update sig_last_seen — newer copy stays canonical
            sig_last_seen[sig] = i

    def filter_turn(entry, age, ctx):
        if entry.role == "tool" and entry.index in suppressed_tool:
            return False

    def transform_turn(entry, age, ctx):
        if entry.role != "assistant":
            return None
        surviving = [
            tc for tc in entry.tool_calls
            if tc["id"] not in suppressed_calls
        ]
        if len(surviving) == len(entry.tool_calls):
            return None
        if not surviving and not entry.content.strip():
            return None
        return _copy(entry, tool_calls=surviving)

    context.register_hook("pre_assemble",   pre_assemble,   priority=0)
    context.register_hook("filter_turn",    filter_turn,    priority=0)
    context.register_hook("transform_turn", transform_turn, priority=0)


# ---------------------------------------------------------------------------
# CoT strip — removes <think>…</think> from assistant turns
# ---------------------------------------------------------------------------

# Matches <think>…</think> (case-insensitive, dotall so newlines are included).
_COT_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _strip_cot(text: str) -> str:
    """Remove all <think>…</think> blocks and collapse leftover blank lines."""
    stripped = _COT_RE.sub("", text)
    # Collapse runs of 3+ newlines down to 2 (one blank line).
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def _register_cot_strip(context, config):
    keep_recent = int(config.get("cot_keep_recent_turns", 0))

    # We need to count only assistant turns, not all turns.
    # age (passed by context) is turns-since-this-entry in the full dialogue,
    # so we track assistant-turn age ourselves via a pre_assemble hook that
    # builds a lookup: entry.index -> assistant_age
    # (0 = most recent assistant turn, 1 = second most recent, …)
    assistant_age: dict[int, int] = {}

    def pre_assemble(ctx):
        assistant_age.clear()
        rank = 0
        for entry in reversed(ctx.dialogue):
            if entry.role == "assistant":
                assistant_age[entry.index] = rank
                rank += 1

    def transform_turn(entry, age, ctx):
        if entry.role != "assistant":
            return None
        if not entry.content:
            return None

        a_age = assistant_age.get(entry.index, 0)
        if a_age < keep_recent:
            return None  # within the protected window — leave intact

        new_content = _strip_cot(entry.content)
        if new_content == entry.content:
            return None  # nothing to strip
        return _copy(entry, content=new_content)

    # priority=5 — after dedup (0), before trim (10)
    context.register_hook("pre_assemble",   pre_assemble,   priority=5)
    context.register_hook("transform_turn", transform_turn, priority=5)


def _register_trim(context, config):
    trim_after     = config.get("tool_trim_after", 10)
    truncate_after = config.get("tool_output_truncate_after", 2)
    max_chars      = config.get("max_tool_output_chars", 2000)

    def transform_turn(entry, age, ctx):
        if entry.role != "tool":
            return None

        if age > trim_after:
            return _copy(entry, content=f"[trimmed — tool output, {age} turns ago]")

        if age > truncate_after and len(entry.content) > max_chars:
            half    = max_chars // 2
            omitted = len(entry.content) - max_chars
            content = (
                entry.content[:half]
                + f"\n... [{omitted} chars omitted] ...\n"
                + entry.content[-half:]
            )
            return _copy(entry, content=content)

        return None

    context.register_hook("transform_turn", transform_turn, priority=10)


def _register_tokenade(context, config):
    """
    Tokenade defense: replace any turn whose content exceeds `tokenade_threshold`
    tokens with a stub message. Runs as a transform_turn hook at priority 1
    (before dedup/trim) so oversized content never reaches the LLM.

    The threshold is compared against the raw token count of the turn's text
    content only (tool_calls JSON is not counted — those are structured and
    bounded by the model). Only user and assistant text content is checked;
    tool results are also checked since they are a common injection vector.
    """
    import logging
    import tiktoken

    threshold = int(config.get("tokenade_threshold", 20000))
    logger = logging.getLogger(__name__)

    # Reuse the same encoder strategy as context.py
    _enc = None

    def _get_enc():
        nonlocal _enc
        if _enc is None:
            try:
                _enc = tiktoken.get_encoding("o200k_base")
            except Exception:
                _enc = None
        return _enc

    def _token_count(text: str) -> int:
        enc = _get_enc()
        if enc is None:
            return len(text) // 4
        return len(enc.encode(text, disallowed_special=()))

    def transform_turn(entry, age, ctx):
        # Only inspect roles that carry free-form user-supplied text.
        if entry.role not in ("user", "assistant", "tool"):
            return None

        content = entry.content
        if isinstance(content, list):
            # Attachment block list — concatenate text parts for counting.
            text_parts = [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = " ".join(text_parts)
        else:
            text = content or ""

        count = _token_count(text)
        if count < threshold:
            return None

        logger.warning(
            "[tokenade] blocked turn (role=%s, index=%d, ~%d tokens > threshold %d)",
            entry.role, entry.index, count, threshold,
        )
        stub = f"[Suspected Tokenade Blocked. Blocked ~{count} tokens.]"
        return _copy(entry, content=stub, tool_calls=[])

    # priority=1 — runs immediately after dedup pre_assemble (0) resolves
    # suppressed sets, but before CoT strip (5) and trim (10).
    context.register_hook("transform_turn", transform_turn, priority=1)


def _copy(entry, **overrides):
    """Return a shallow copy of a HistoryEntry with fields overridden."""
    from TinyCTX.context import HistoryEntry
    return HistoryEntry(
        role=overrides.get("role", entry.role),
        content=overrides.get("content", entry.content),
        id=entry.id,
        index=entry.index,
        tool_calls=overrides.get("tool_calls", entry.tool_calls),
        tool_call_id=entry.tool_call_id,
    )
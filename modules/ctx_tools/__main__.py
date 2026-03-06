from __future__ import annotations
import json
import re


def register(agent, config=None):
    if config is None:
        try:
            from modules.ctx_tools import EXTENSION_META
            config = EXTENSION_META.get("default_config", {})
        except ImportError:
            config = {}
    _register_dedup(agent.context, config)
    _register_cot_strip(agent.context, config)
    _register_trim(agent.context, config)


def _register_dedup(context, config):
    dedup_after = config.get("same_call_dedup_after", 3)

    suppressed_tool:  set[int] = set()
    suppressed_calls: set[str] = set()

    def pre_assemble(ctx):
        suppressed_tool.clear()
        suppressed_calls.clear()

        dialogue = ctx.dialogue
        n = len(dialogue)

        call_map = {
            tc["id"]: tc
            for entry in dialogue
            for tc in entry.tool_calls
        }

        seen: set[str] = set()

        for i in reversed(range(n)):
            entry = dialogue[i]
            if entry.role != "tool" or not entry.tool_call_id:
                continue
            tc = call_map.get(entry.tool_call_id)
            if not tc:
                continue
            sig = tc["name"] + "::" + json.dumps(tc["arguments"], sort_keys=True)
            age = n - 1 - i
            if sig in seen and age > dedup_after:
                suppressed_tool.add(i)
                suppressed_calls.add(tc["id"])
            else:
                seen.add(sig)

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


def _copy(entry, **overrides):
    """Return a shallow copy of a HistoryEntry with fields overridden."""
    from context import HistoryEntry
    return HistoryEntry(
        role=overrides.get("role", entry.role),
        content=overrides.get("content", entry.content),
        id=entry.id,
        index=entry.index,
        tool_calls=overrides.get("tool_calls", entry.tool_calls),
        tool_call_id=entry.tool_call_id,
    )
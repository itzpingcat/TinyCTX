from __future__ import annotations
import json
import re


def register_runtime(runtime) -> None:
    """Register ctx_tools context hooks into the module registry."""
    # ctx_tools only registers context hooks — no singletons, no tools.
    # Nothing to do at runtime level; all wiring happens per-cycle.
    pass


def register_agent(cycle) -> None:
    """Wire ctx_tools context hooks into this AgentCycle's context."""
    try:
        from TinyCTX.modules.ctx_tools import EXTENSION_META
        config = EXTENSION_META.get("default_config", {})
    except ImportError:
        config = {}
    _register_dedup(cycle.context, config)
    _register_cot_strip(cycle.context, config)
    _register_trim(cycle.context, config)
    _register_tokenade(cycle.context, config)


def register(runtime, config=None):
    """Legacy shim."""
    from TinyCTX.runtime import Runtime as _Runtime
    if isinstance(runtime, _Runtime):
        register_runtime(runtime)
    else:
        # Called with a cycle-like object
        register_agent(runtime)


def _register_dedup(context, config):
    dedup_after = config.get("same_call_dedup_after", 3)

    suppressed_tool:  set[int] = set()
    suppressed_calls: set[str] = set()

    def pre_assemble(ctx):
        suppressed_tool.clear()
        suppressed_calls.clear()

        dialogue = ctx.dialogue
        n = len(dialogue)

        call_map: dict[str, dict] = {}
        for entry in dialogue:
            for tc in entry.tool_calls:
                call_map[tc["id"]] = tc

        sig_last_seen: dict[str, int] = {}

        for i in reversed(range(n)):
            entry = dialogue[i]
            if entry.role != "tool" or not entry.tool_call_id:
                continue
            tc = call_map.get(entry.tool_call_id)
            if not tc:
                suppressed_tool.add(i)
                continue
            sig = tc["name"] + "::" + json.dumps(tc["arguments"], sort_keys=True)
            if sig in sig_last_seen:
                distance = sig_last_seen[sig] - i
                if distance > dedup_after:
                    suppressed_tool.add(i)
                    suppressed_calls.add(tc["id"])
                    continue
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


_COT_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _strip_cot(text: str) -> str:
    stripped = _COT_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def _register_cot_strip(context, config):
    keep_recent = int(config.get("cot_keep_recent_turns", 0))

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
            return None

        new_content = _strip_cot(entry.content)
        if new_content == entry.content:
            return None
        return _copy(entry, content=new_content)

    context.register_hook("pre_assemble",   pre_assemble,   priority=5)
    context.register_hook("transform_turn", transform_turn, priority=5)


def _register_trim(context, config):
    trim_after     = config.get("tool_trim_after", 10)
    truncate_after = config.get("tool_output_truncate_after", 2)
    max_chars      = config.get("max_tool_output_chars", 2000)

    trimmed_calls: set[str] = set()

    def pre_assemble(ctx):
        trimmed_calls.clear()
        dialogue = ctx.dialogue
        n = len(dialogue)

        call_map: dict[str, dict] = {}
        for entry in dialogue:
            for tc in entry.tool_calls:
                call_map[tc["id"]] = tc

        for i in range(n):
            entry = dialogue[i]
            if entry.role != "tool" or not entry.tool_call_id:
                continue
            age = n - 1 - i
            if age > trim_after:
                trimmed_calls.add(entry.tool_call_id)

    def transform_turn(entry, age, ctx):
        if entry.role == "assistant":
            if not trimmed_calls:
                return None
            surviving = [
                tc for tc in entry.tool_calls
                if tc["id"] not in trimmed_calls
            ]
            if len(surviving) == len(entry.tool_calls):
                return None
            if not surviving and not entry.content.strip():
                return None
            return _copy(entry, tool_calls=surviving)

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

    context.register_hook("pre_assemble",   pre_assemble,   priority=8)
    context.register_hook("transform_turn", transform_turn, priority=10)


def _register_tokenade(context, config):
    import logging
    import tiktoken

    threshold = int(config.get("tokenade_threshold", 20000))
    _logger = logging.getLogger(__name__)

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
        if entry.role not in ("user", "assistant", "tool"):
            return None

        content = entry.content
        if isinstance(content, list):
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

        _logger.warning(
            "[tokenade] blocked turn (role=%s, index=%d, ~%d tokens > threshold %d)",
            entry.role, entry.index, count, threshold,
        )
        stub = f"[Suspected Tokenade Blocked. Blocked ~{count} tokens.]"
        return _copy(entry, content=stub, tool_calls=[])

    context.register_hook("transform_turn", transform_turn, priority=1)


def _copy(entry, **overrides):
    from TinyCTX.context import HistoryEntry
    return HistoryEntry(
        role=overrides.get("role", entry.role),
        content=overrides.get("content", entry.content),
        id=entry.id,
        index=entry.index,
        tool_calls=overrides.get("tool_calls", entry.tool_calls),
        tool_call_id=entry.tool_call_id,
    )

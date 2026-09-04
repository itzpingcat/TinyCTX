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
    _register_label_prefix_strip(cycle, config)


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
    mode = config.get("trim_thinking", "auto")

    # In "auto" mode: the index of the most recent user entry — every
    # assistant entry AFTER it belongs to the agentcycle still in progress
    # (that cycle's own tool-call/assistant turns, all newer than the user
    # message that started it) and keeps its thinking; every assistant
    # entry at or before it is from a prior, finished cycle and gets
    # stripped. -1 (nothing kept) when there's no user entry yet.
    last_user_idx: list[int] = [-1]

    def pre_assemble(ctx):
        last_user_idx[0] = next(
            (i for i in range(len(ctx.dialogue) - 1, -1, -1)
             if ctx.dialogue[i].role == "user"),
            -1,
        )

    def transform_turn(entry, age, ctx):
        if mode == "none":
            return None
        if entry.role != "assistant":
            return None
        if not entry.content:
            return None

        if mode == "auto" and entry.index > last_user_idx[0]:
            return None  # still in this agentcycle — keep it

        new_content = _strip_cot(entry.content)
        if new_content == entry.content:
            return None
        return _copy(entry, content=new_content)

    context.register_hook("pre_assemble",   pre_assemble,   priority=5)
    context.register_hook("transform_turn", transform_turn, priority=5)


def _register_trim(context, config):
    tool_output_cfg = config.get("tool_output", {})
    trim_after     = tool_output_cfg.get("trim_after", 10)
    truncate_after = tool_output_cfg.get("truncate_after", 2)
    max_chars      = tool_output_cfg.get("max_chars", 2000)

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
            # Content is being fully discarded — any tag this entry carried
            # (e.g. "skill:foo" from use_skill) no longer describes anything
            # real, so it must not survive into AssembleMeta.invalidated_tags
            # as "present."
            return _copy(entry, content=f"[trimmed — tool output, {age} turns ago]", tags=frozenset())

        if age > truncate_after and len(entry.content) > max_chars:
            half    = max_chars // 2
            omitted = len(entry.content) - max_chars
            content = (
                entry.content[:half]
                + f"\n... [{omitted} chars omitted] ...\n"
                + entry.content[-half:]
            )
            # Truncation is also destructive to the tagged content — clear tags.
            return _copy(entry, content=content, tags=frozenset())

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
        return _copy(entry, content=stub, tool_calls=[], tags=frozenset())

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
        tags=overrides.get("tags", entry.tags),
    )


# ---------------------------------------------------------------------------
# label_prefix_strip -- AgentCycle.stream_text_hooks (see agent.py __init__)
# ---------------------------------------------------------------------------
#
# context.py's assemble() injects "【{author_id}】: " as a prefix on USER
# turns only, to attribute speakers in multi-participant chats (see
# context.py's assemble(), ~line 744: f"【{label}】: "). It must never
# appear on an assistant turn. Models occasionally imitate the pattern
# in-context and start echoing "【SomeName】: " at the head of their own
# replies; once that lands in stored history it reinforces itself on every
# later turn, since the model now sees its own past labeled replies as
# precedent. This hook buffers the start of each streamed reply just long
# enough to strip a leading label before any text reaches a client, so the
# pattern never enters a live transcript and can't compound turn over turn.

# _PREFIX_ONLY_RE: the buffer so far is exactly "【label】:" plus (maybe only
# some of the) trailing whitespace, with no body text yet -- keep buffering
# rather than resolving, since the separator space in context.py's
# f"【{label}】: " can itself arrive split across TextDelta chunks.
# _PREFIX_STRIP_RE: same shape, used once body text has arrived, to cut the
# prefix off the front of the buffer.
_LABEL_PREFIX_ONLY_RE  = re.compile(r"^【[^【】]{0,32}】:\s*$")
_LABEL_PREFIX_STRIP_RE = re.compile(r"^【[^【】]{0,32}】:\s*")


class _LabelPrefixStripHook:
    """
    Implements AgentCycle.stream_text_hooks' reset()/process()/flush()
    protocol. Operates on accumulated text rather than raw provider chunks,
    so it's correct regardless of how a delta stream happens to split the
    brackets, colon, or separator space across tokens.
    """

    def __init__(self, max_buffer: int):
        self._max_buffer = max_buffer
        self._buf = ""
        self._resolved = False

    def reset(self) -> None:
        self._buf = ""
        self._resolved = False

    def process(self, text: str) -> str:
        if self._resolved:
            return text

        self._buf += text
        if not self._buf.startswith("【"):
            # Can never become a "【label】: " prefix -- no reason to
            # hold ordinary replies back waiting for the cap or a newline.
            self._resolved = True
            out, self._buf = self._buf, ""
            return out

        if _LABEL_PREFIX_ONLY_RE.match(self._buf):
            # Buffer is just "【label】:" (+ maybe partial trailing
            # whitespace) with no body text yet -- keep waiting.
            if len(self._buf) >= self._max_buffer:
                self._resolved = True
                out, self._buf = self._buf, ""
                return out
            return ""

        stripped = _LABEL_PREFIX_STRIP_RE.sub("", self._buf, count=1)
        if stripped != self._buf:
            # Prefix matched with real body text after it -- drop the
            # prefix, release the body.
            self._resolved = True
            self._buf = ""
            return stripped

        if len(self._buf) >= self._max_buffer or "\n" in self._buf:
            # No match possible within the buffer budget (or the model
            # moved past the first line without opening with 【) -- give up
            # waiting, release as-is.
            self._resolved = True
            out, self._buf = self._buf, ""
            return out

        return ""  # keep buffering, nothing to release yet

    def flush(self) -> str:
        # Stream ended (or errored) before the buffer resolved -- e.g. a
        # short reply that finished mid-buffer with no newline. Apply the
        # same check once more before releasing whatever's left.
        if self._resolved or not self._buf:
            self._buf = ""
            return ""
        stripped = _LABEL_PREFIX_STRIP_RE.sub("", self._buf, count=1)
        self._buf = ""
        return stripped


def _register_label_prefix_strip(cycle, config):
    max_buffer = config.get("label_prefix_strip_max_chars", 40)
    cycle.stream_text_hooks.append(_LabelPrefixStripHook(max_buffer))

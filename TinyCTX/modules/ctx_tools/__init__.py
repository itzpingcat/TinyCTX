from __future__ import annotations

import json
import logging
import re

from TinyCTX.decorators import hook
from TinyCTX.hooks import HookType
from TinyCTX.module import Module

logger = logging.getLogger(__name__)


class CtxTools(Module):
    """Core context optimizations: dedup, CoT strip, and trim."""

    settings = {
        "same_call_dedup_after": {
            "default": 2,
            "type": "int",
            "description": "An identical tool call repeated after this many turns has its earlier copies dropped.",
        },
        "trim_thinking": {
            "default": "auto",
            "type": "select",
            "options": {
                "all": "Strip every <think>...</think> block, including the one just produced this cycle.",
                "auto": "Keep <think> blocks belonging to the agentcycle still in progress; strip everything older.",
                "none": "Never strip; every stored <think> block stays, subject to normal token-budget trimming.",
            },
            "description": "How much <think> content survives into later turns.",
        },
        "tokenade_threshold": {
            "default": 20000,
            "type": "int",
            "description": "Token count above which a single turn's content is replaced with a blocked-content stub.",
        },
        "label_prefix_strip_max_chars": {
            "default": 40,
            "type": "int",
            "description": "Max chars of a streamed reply's opening text buffered while checking for a spoofed speaker-label prefix.",
        },
        "tool_output": {
            "default": {"trim_after": 25, "truncate_after": 10, "max_chars": 2000},
            "type": "dict",
            "description": "trim_after/truncate_after (turns old) and max_chars governing tool-output aging.",
        },
    }

    def __init__(self) -> None:
        self._enc = None

    # -----------------------------------------------------------------
    # dedup — same tool call repeated beyond same_call_dedup_after turns
    # -----------------------------------------------------------------

    @hook(HookType.PRE_ASSEMBLE, priority=0)
    def dedup_scan(self, ctx, scratch):
        dedup_after = self.config["same_call_dedup_after"]
        scratch.suppressed_tool = set()
        scratch.suppressed_calls = set()

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
                scratch.suppressed_tool.add(i)
                continue
            sig = tc["name"] + "::" + json.dumps(tc["arguments"], sort_keys=True)
            if sig in sig_last_seen:
                distance = sig_last_seen[sig] - i
                if distance > dedup_after:
                    scratch.suppressed_tool.add(i)
                    scratch.suppressed_calls.add(tc["id"])
                    continue
            sig_last_seen[sig] = i

    @hook(HookType.FILTER_TURN, priority=0)
    def dedup_filter(self, entry, age, ctx, scratch):
        if entry.role == "tool" and entry.index in scratch.suppressed_tool:
            return False

    @hook(HookType.TRANSFORM_TURN, priority=0)
    def dedup_transform(self, entry, age, ctx, scratch):
        if entry.role != "assistant":
            return None
        surviving = [tc for tc in entry.tool_calls if tc["id"] not in scratch.suppressed_calls]
        if len(surviving) == len(entry.tool_calls):
            return None
        if not surviving and not entry.content.strip():
            return None
        return _copy(entry, tool_calls=surviving)

    # -----------------------------------------------------------------
    # cot_strip — drop <think> blocks from finished agentcycles
    # -----------------------------------------------------------------

    @hook(HookType.PRE_ASSEMBLE, priority=5)
    def cot_strip_scan(self, ctx, scratch):
        # Index of the most recent user entry: every assistant entry after it
        # belongs to the agentcycle still in progress and keeps its thinking;
        # everything at or before it is a finished prior cycle. -1 (nothing
        # kept) when there's no user entry yet.
        scratch.last_user_idx = next(
            (i for i in range(len(ctx.dialogue) - 1, -1, -1) if ctx.dialogue[i].role == "user"),
            -1,
        )

    @hook(HookType.TRANSFORM_TURN, priority=5)
    def cot_strip_transform(self, entry, age, ctx, scratch):
        mode = self.config["trim_thinking"]
        if mode == "none":
            return None
        if entry.role != "assistant" or not entry.content:
            return None
        if mode == "auto" and entry.index > scratch.last_user_idx:
            return None  # still in this agentcycle — keep it
        new_content = _strip_cot(entry.content)
        if new_content == entry.content:
            return None
        return _copy(entry, content=new_content)

    # -----------------------------------------------------------------
    # trim — age out and truncate old tool output
    # -----------------------------------------------------------------

    @hook(HookType.PRE_ASSEMBLE, priority=8)
    def trim_scan(self, ctx, scratch):
        trim_after = self.config["tool_output"].get("trim_after", 10)
        scratch.trimmed_calls = set()
        dialogue = ctx.dialogue
        n = len(dialogue)
        for i in range(n):
            entry = dialogue[i]
            if entry.role != "tool" or not entry.tool_call_id:
                continue
            if n - 1 - i > trim_after:
                scratch.trimmed_calls.add(entry.tool_call_id)

    @hook(HookType.TRANSFORM_TURN, priority=10)
    def trim_transform(self, entry, age, ctx, scratch):
        tool_output_cfg = self.config["tool_output"]
        trim_after = tool_output_cfg.get("trim_after", 10)
        truncate_after = tool_output_cfg.get("truncate_after", 2)
        max_chars = tool_output_cfg.get("max_chars", 2000)

        if entry.role == "assistant":
            if not scratch.trimmed_calls:
                return None
            surviving = [tc for tc in entry.tool_calls if tc["id"] not in scratch.trimmed_calls]
            if len(surviving) == len(entry.tool_calls):
                return None
            if not surviving and not entry.content.strip():
                return None
            return _copy(entry, tool_calls=surviving)

        if entry.role != "tool":
            return None

        if age > trim_after:
            # Content is fully discarded — any tag this entry carried (e.g.
            # "skill:foo" from use_skill) no longer describes anything real,
            # so it must not survive into AssembleMeta.invalidated_tags as
            # "present."
            return _copy(entry, content=f"[trimmed — tool output, {age} turns ago]", tags=frozenset())

        if age > truncate_after and len(entry.content) > max_chars:
            half = max_chars // 2
            omitted = len(entry.content) - max_chars
            content = (
                entry.content[:half]
                + f"\n... [{omitted} chars omitted] ...\n"
                + entry.content[-half:]
            )
            return _copy(entry, content=content, tags=frozenset())  # truncation is also destructive — clear tags

        return None

    # -----------------------------------------------------------------
    # tokenade — block turns whose content exceeds a token threshold
    # -----------------------------------------------------------------

    @hook(HookType.TRANSFORM_TURN, priority=1)
    def tokenade_transform(self, entry, age, ctx):
        if entry.role not in ("user", "assistant", "tool"):
            return None

        content = entry.content
        if isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = content or ""

        count = self._token_count(text)
        threshold = self.config["tokenade_threshold"]
        if count < threshold:
            return None

        logger.warning(
            "[tokenade] blocked turn (role=%s, index=%d, ~%d tokens > threshold %d)",
            entry.role, entry.index, count, threshold,
        )
        stub = f"[Suspected Tokenade Blocked. Blocked ~{count} tokens.]"
        return _copy(entry, content=stub, tool_calls=[], tags=frozenset())

    def _get_enc(self):
        if self._enc is None:
            try:
                import tiktoken
                self._enc = tiktoken.get_encoding("o200k_base")
            except Exception:
                self._enc = None
        return self._enc

    def _token_count(self, text: str) -> int:
        enc = self._get_enc()
        if enc is None:
            return len(text) // 4
        return len(enc.encode(text, disallowed_special=()))

    # -----------------------------------------------------------------
    # label_prefix_strip — AgentCycle.stream_text_hooks, see _LabelPrefixStripHook
    # -----------------------------------------------------------------

    @hook(HookType.TURN_START, priority=0)
    def wire_stream_hooks(self, cycle) -> None:
        max_buffer = self.config["label_prefix_strip_max_chars"]
        cycle.stream_text_hooks.append(_LabelPrefixStripHook(max_buffer))


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


_COT_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _strip_cot(text: str) -> str:
    stripped = _COT_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


# ---------------------------------------------------------------------------
# label_prefix_strip -- AgentCycle.stream_text_hooks (see agent.py __init__)
# ---------------------------------------------------------------------------
#
# context.py's assemble() injects "【{author_id}】: " as a prefix on USER
# turns only, to attribute speakers in multi-participant chats (see
# context.py's assemble(), ~line 744: f"【{label}】: "). It must never
# appear on an assistant turn. Models occasionally imitate the pattern
# in-context and start echoing "【SomeName】: " at the head of a line in
# their own replies -- not only at the very start of the reply, but at the
# start of later lines too (e.g. one label per paragraph, or per "turn" in
# a simulated multi-speaker exchange). Once that lands in stored history it
# reinforces itself on every later turn, since the model now sees its own
# past labeled replies as precedent. This hook buffers the start of each
# line of a streamed reply just long enough to strip a leading label
# before any text reaches a client, so the pattern never enters a live
# transcript and can't compound turn over turn.
#
# State is tracked per-line rather than per-stream: after any line resolves
# (matched-and-stripped, or given up on), the hook re-arms and watches the
# start of the next line for the same pattern, all the way to flush().

# _PREFIX_ONLY_RE: the line-start buffer so far is exactly "【label】:" plus
# (maybe only some of the) trailing whitespace, with no body text yet --
# keep buffering rather than resolving, since the separator space in
# context.py's f"【{label}】: " can itself arrive split across TextDelta
# chunks.
# _PREFIX_STRIP_RE: same shape, used once body text has arrived, to cut the
# prefix off the front of the line-start buffer.
_LABEL_PREFIX_ONLY_RE = re.compile(r"^【[^【】]{0,32}】:\s*$")
_LABEL_PREFIX_STRIP_RE = re.compile(r"^【[^【】]{0,32}】:\s*")


class _LabelPrefixStripHook:
    """
    Implements AgentCycle.stream_text_hooks' reset()/process()/flush()
    protocol. Operates on accumulated text rather than raw provider chunks,
    so it's correct regardless of how a delta stream happens to split the
    brackets, colon, separator space, or newline across tokens.

    Re-arms at the start of every line (not just the start of the stream),
    since the model can repeat the "【label】: " pattern at the head of any
    line, not only the first.
    """

    def __init__(self, max_buffer: int):
        self._max_buffer = max_buffer
        self._buf = ""       # line-start candidate buffer, watched for a prefix
        self._armed = True   # True while at a line-start still being checked

    def reset(self) -> None:
        self._buf = ""
        self._armed = True

    def process(self, text: str) -> str:
        out_parts: list[str] = []

        while text:
            if not self._armed:
                # Mid-line, not currently checking for a prefix -- pass
                # through up to (and including) the next newline, then
                # re-arm for the line that follows it.
                nl = text.find("\n")
                if nl == -1:
                    out_parts.append(text)
                    text = ""
                else:
                    out_parts.append(text[: nl + 1])
                    text = text[nl + 1 :]
                    self._armed = True
                    self._buf = ""
                continue

            self._buf += text
            text = ""

            if not self._buf.startswith("【"):
                # Can never become a "【label】: " prefix -- no reason to
                # hold this line back waiting for the cap or a newline.
                # Re-feed it through the unarmed path (rather than emitting
                # directly) in case it already contains a "\n" that starts
                # a new line needing its own check.
                self._armed = False
                text = self._buf
                self._buf = ""
                continue

            if _LABEL_PREFIX_ONLY_RE.match(self._buf):
                # Buffer is just "【label】:" (+ maybe partial trailing
                # whitespace) with no body text yet -- keep waiting.
                if len(self._buf) >= self._max_buffer:
                    self._armed = False
                    text = self._buf
                    self._buf = ""
                continue

            stripped = _LABEL_PREFIX_STRIP_RE.sub("", self._buf, count=1)
            if stripped != self._buf:
                # Prefix matched with real body text after it -- drop the
                # prefix, release the body, then keep processing whatever
                # of that body (possibly another newline) remains.
                self._armed = False
                text = stripped
                self._buf = ""
                continue

            if len(self._buf) >= self._max_buffer or "\n" in self._buf:
                # No match possible within the buffer budget (or the model
                # moved past the first line of this segment without
                # opening with 【) -- give up waiting, release as-is (again
                # re-fed, in case a "\n" inside it starts a new line).
                self._armed = False
                text = self._buf
                self._buf = ""
                continue

            # keep buffering, nothing to release yet this round

        return "".join(out_parts)

    def flush(self) -> str:
        # Stream ended (or errored) before the current line resolved --
        # e.g. a short reply that finished mid-buffer with no newline.
        # Apply the same check once more before releasing whatever's left.
        if not self._armed or not self._buf:
            self._buf = ""
            self._armed = True
            return ""
        stripped = _LABEL_PREFIX_STRIP_RE.sub("", self._buf, count=1)
        self._buf = ""
        self._armed = True
        return stripped

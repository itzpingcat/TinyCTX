"""
modules/output_parser/parser.py

Detects tool-call-shaped content embedded in assistant message TEXT — the
model had a native tool-calling channel available and either slipped into
writing the call as text, or (for Pythonic-format models like LFM2/Liquid)
is using text because that IS its native channel and the backend isn't
parsing it upstream.

Ported from little-coder's .pi/extensions/output-parser (issue #42's Pythonic
handling in particular) — same format set, same reasoning for why each format
needs its own tolerant parser instead of one generic json.loads.

No execution happens here. This module only detects and describes; the
caller (output_parser/__main__.py) decides what to do about it.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass


@dataclass
class ParsedToolCall:
    name:   str
    input:  dict
    format: str  # "fenced" | "tag" | "bare_json" | "liquid"


# ---------------------------------------------------------------------------
# Fenced ```tool_call / ```json blocks containing {"name": ..., "arguments"/"input": ...}
# ---------------------------------------------------------------------------

_FENCED_RE = re.compile(
    r"```(?:tool_call|tool|json)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# <tool_call>...</tool_call> XML-ish tags (a very common local-model artifact,
# esp. Qwen/Hermes-style templates that leak the tag into content instead of
# the backend consuming it).
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Bare top-level JSON object that looks like a call, not wrapped in fences
# or tags. Deliberately narrow (must contain a "name" key) so we don't
# misfire on the model just discussing JSON in prose.
#
# A single regex can't match nested braces (arguments/input is itself an
# object), so this is a brace-counting scan seeded at each `"name"` sighting
# rather than one _BARE_JSON_RE pattern.
# ---------------------------------------------------------------------------

_NAME_KEY_RE = re.compile(r"\"name\"\s*:\s*\"[^\"]+\"")


def _find_bare_json_spans(text: str) -> list[tuple[int, int]]:
    """Find balanced {...} spans that contain a "name" key, by walking
    outward from each opening brace that precedes a "name" sighting."""
    spans: list[tuple[int, int]] = []
    for name_match in _NAME_KEY_RE.finditer(text):
        # Find the nearest unclosed '{' at or before this "name" key by
        # scanning left with a running depth count.
        start = None
        depth = 0
        i = name_match.start() - 1
        while i >= 0:
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth == 0:
                    start = i
                    break
                depth -= 1
            i -= 1
        if start is None:
            continue
        # Now scan forward from `start` counting braces to find the matching close.
        depth = 0
        end = None
        for j in range(start, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end is None:
            continue
        span = (start, end)
        if span not in spans:
            spans.append(span)
    return spans

# ---------------------------------------------------------------------------
# Pythonic / LFM2-Liquid format: <|tool_call_start|>[Name(arg='val', ...)]<|tool_call_end|>
# This IS that model family's native tool-calling channel — see module
# docstring. We still need to detect it (to route to the diagnostic path
# instead of the nudge path), just not to "fix" it via a nudge.
# ---------------------------------------------------------------------------

_LIQUID_RE = re.compile(
    r"<\|tool_call_start\|>\s*\[(.*?)\]\s*<\|tool_call_end\|>",
    re.DOTALL,
)
_LIQUID_CALL_RE = re.compile(r"(\w+)\((.*)\)\s*$", re.DOTALL)


def _try_json_loads(text: str) -> dict | None:
    """json.loads with a couple of cheap repairs for common small-model
    slips (trailing commas, single-quoted strings) before giving up."""
    text = text.strip()
    for candidate in (text, _repair_trailing_commas(text)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    try:
        # ast.literal_eval tolerates single-quoted dict-like text that isn't
        # valid JSON but is valid Python — a common local-model artifact.
        val = ast.literal_eval(text)
        return val if isinstance(val, dict) else None
    except (ValueError, SyntaxError):
        return None


def _repair_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _extract_call_from_obj(obj: dict) -> ParsedToolCall | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not name or not isinstance(name, str):
        return None
    args = obj.get("arguments") or obj.get("input") or obj.get("parameters") or {}
    if not isinstance(args, dict):
        return None
    return ParsedToolCall(name=name, input=args, format="")  # format set by caller


def _parse_liquid_call(inner: str) -> ParsedToolCall | None:
    """Parse `Name(arg='val', other=123)` — Python-call-expression syntax."""
    match = _LIQUID_CALL_RE.match(inner.strip())
    if not match:
        return None
    name, argstr = match.group(1), match.group(2)
    try:
        # Wrap as a call expression so ast can parse keyword args safely,
        # without eval()'ing arbitrary code.
        parsed = ast.parse(f"_({argstr})", mode="eval")
        call = parsed.body
        if not isinstance(call, ast.Call):
            return None
        kwargs = {}
        for kw in call.keywords:
            if kw.arg is None:
                continue
            kwargs[kw.arg] = ast.literal_eval(kw.value)
        return ParsedToolCall(name=name, input=kwargs, format="liquid")
    except (SyntaxError, ValueError):
        return None


def parse_text_tool_calls(text: str) -> list[ParsedToolCall]:
    """
    Scan assistant message text for tool-call-shaped content. Returns every
    call found, tagged with the format it was found in. Best-effort: a
    fragment that looks close to a call but doesn't fully parse is skipped
    rather than raising — this runs on every completion, so it must never
    be the thing that breaks a turn.
    """
    if not text or "(" not in text and "{" not in text and "<" not in text:
        return []  # cheap short-circuit — none of our patterns can match

    calls: list[ParsedToolCall] = []
    consumed_spans: list[tuple[int, int]] = []

    def _overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < e and s < span[1] for s, e in consumed_spans)

    # Liquid/Pythonic — checked first since its delimiters are the most
    # specific and we don't want a fenced/tag pass to eat part of it first.
    for m in _LIQUID_RE.finditer(text):
        if _overlaps(m.span()):
            continue
        call = _parse_liquid_call(m.group(1))
        if call:
            calls.append(call)
            consumed_spans.append(m.span())

    for m in _TAG_RE.finditer(text):
        if _overlaps(m.span()):
            continue
        obj = _try_json_loads(m.group(1))
        call = _extract_call_from_obj(obj) if obj else None
        if call:
            calls.append(ParsedToolCall(call.name, call.input, format="tag"))
            consumed_spans.append(m.span())

    for m in _FENCED_RE.finditer(text):
        if _overlaps(m.span()):
            continue
        obj = _try_json_loads(m.group(1))
        call = _extract_call_from_obj(obj) if obj else None
        if call:
            calls.append(ParsedToolCall(call.name, call.input, format="fenced"))
            consumed_spans.append(m.span())

    for span in _find_bare_json_spans(text):
        if _overlaps(span):
            continue
        obj = _try_json_loads(text[span[0]:span[1]])
        call = _extract_call_from_obj(obj) if obj else None
        if call:
            calls.append(ParsedToolCall(call.name, call.input, format="bare_json"))
            consumed_spans.append(span)

    return calls

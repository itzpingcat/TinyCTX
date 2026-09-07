"""
bridges/discord/quote_reply.py — split an agent's final reply text into
per-quote reply segments for Discord.

Feature: when the agent's response contains one or more Markdown blockquote
blocks (consecutive lines starting with `>` -- only recognized at the start
of a line; `>` appearing mid-line, e.g. inside "a>b>c", is not a quote
marker and never breaks the regex), each block is treated as a
back-reference to some recent message in the channel. We regex the block's
content against recent channel history to find which real message it's
quoting, and send the prose that FOLLOWS each quote block as its own
Discord reply targeting the resolved message. Text before the first quote
block (if any) is sent as a single plain (non-reply) message.

If a quote block resolves to a real message, the literal '> ...' block is
stripped from the visible text (Discord's reply UI already shows the jump
to the quoted message, so keeping it would be redundant). If a quote block
does NOT resolve to any recent message, we're not confident it was even a
back-reference -- so it's kept verbatim in the sent text rather than
silently dropped.

This is Discord-only, deliberately: the core agent/runtime code has no
notion of "replies" — it just produces text. All quote-parsing and
reply-resolution lives here so it can be enabled/disabled/tuned per bridge
without touching contracts.py or agent.py.

Segmentation rule (forward-binding): a quote block's segment is the text
between IT and the NEXT quote block (or end of string) — never the text
before it. Given:

    Sure, on that first point:
    > hi there
    got it, thanks!
    > what about tomorrow
    let me check on that

this yields three segments:
    1. plain:                    "Sure, on that first point:"
    2. reply to "hi there" msg:  "got it, thanks!"
    3. reply to "what about..."  "let me check on that"

A quote block with no following prose AND a resolved target produces no
segment for that block — the quote text was stripped and there is no
prose left to send. An unresolved quote block always produces a segment
(target=None) even with no following prose, since its raw text is being
kept and must still be sent somewhere.

ReDoS note: _QUOTE_BLOCK_RE has no nested quantifiers over `.` spanning
newlines — re.MULTILINE keeps `.` from crossing line boundaries, and the
block-continuation is a simple `(?:\n>...)*` one line at a time, so
matching is linear in input length. Callers should still bound input size
(see ChannelRenderer) as defense in depth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# One or more consecutive lines starting with '>' (optionally '> ' with a
# space). MULTILINE so ^ / $ anchor per line; no DOTALL, no nested `.*`
# spanning lines -- each continuation line is matched individually via the
# repeated group, keeping this linear-time.
_QUOTE_BLOCK_RE = re.compile(r"^>[ \t]?.*(?:\n>[ \t]?.*)*", re.MULTILINE)

# Defense-in-depth cap: only scan the tail of very long texts. Agent replies
# are already bounded by model output / max_reply_length, but this keeps the
# regex's input size independent of any future change to those bounds.
_MAX_SCAN_CHARS = 8000


class _HasContent(Protocol):
    content: str


@dataclass(frozen=True)
class ReplySegment:
    """One outgoing Discord message: text to send, and the message (if any)
    it should be sent as a reply to."""
    text: str
    target: "_HasContent | None"


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace, for loose substring matching."""
    return " ".join(text.split()).lower()


def _strip_quote_markers(block: str) -> str:
    """Turn a matched quote block ('> line\\n> line2') into its bare content."""
    lines = []
    for line in block.splitlines():
        # Strip exactly one leading '>' and at most one following space/tab.
        stripped = line[1:]
        if stripped[:1] in (" ", "\t"):
            stripped = stripped[1:]
        lines.append(stripped)
    return "\n".join(lines)


def resolve_target(
    quote_content: str,
    candidates: list,
    min_len: int = 8,
) -> object | None:
    """
    Find which candidate message a quote block is quoting.

    candidates: recent messages, most-recent-first (as discord.py's
    channel.history() yields by default), each with a `.content` attribute.

    Matching is a simple, cheap normalized substring check in either
    direction (handles both truncated quotes and the agent padding/trimming
    the quoted text slightly). Ties go to the most recent candidate. Quotes
    shorter than min_len (after normalization) are never matched -- short
    generic quotes like "> yes" would false-match against almost anything.
    """
    normalized_quote = _normalize(quote_content)
    if len(normalized_quote) < min_len:
        return None

    for candidate in candidates:
        content = getattr(candidate, "content", "") or ""
        normalized_content = _normalize(content)
        if not normalized_content:
            continue
        if normalized_quote in normalized_content or normalized_content in normalized_quote:
            return candidate

    return None


def split_into_reply_segments(
    text: str,
    candidates: list,
    min_len: int = 8,
) -> list[ReplySegment]:
    """
    Split `text` into ReplySegments per the forward-binding rule (see module
    docstring). `candidates` is passed through to resolve_target() unchanged
    for every quote block found.
    """
    scan_text = text[-_MAX_SCAN_CHARS:]

    matches = list(_QUOTE_BLOCK_RE.finditer(scan_text))
    if not matches:
        stripped = text.strip()
        return [ReplySegment(text=stripped, target=None)] if stripped else []

    segments: list[ReplySegment] = []

    # Text before the first quote block: plain message, no target.
    lead_in = scan_text[: matches[0].start()].strip()
    if lead_in:
        segments.append(ReplySegment(text=lead_in, target=None))

    for i, match in enumerate(matches):
        raw_block = match.group(0)
        quote_content = _strip_quote_markers(raw_block)
        target = resolve_target(quote_content, candidates, min_len=min_len)

        seg_start = match.end()
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(scan_text)
        following_text = scan_text[seg_start:seg_end].strip()

        if target is not None:
            # Resolved -- Discord's reply UI already shows the jump to the
            # quoted message, so the literal '> ...' block is redundant and
            # gets stripped from the visible text.
            seg_text = following_text
        else:
            # Unresolved -- we're not confident this was even a back-
            # reference, so keep the quote block verbatim rather than
            # silently dropping content the agent wrote.
            seg_text = f"{raw_block}\n{following_text}".strip() if following_text else raw_block

        if seg_text:
            segments.append(ReplySegment(text=seg_text, target=target))
        # else: quote block with no following prose and a resolved target --
        # nothing to send (the quote itself was stripped, and there's no
        # prose left).

    return segments

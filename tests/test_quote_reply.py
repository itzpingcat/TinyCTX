"""
tests/test_quote_reply.py

Unit tests for bridges/discord/quote_reply.py — the regex-based quote-block
splitter and message-matcher backing Discord's "reply to what I quoted"
feature. Pure-function tests, no discord.py dependency: candidate messages
are simple objects with a `.content` attribute, matching what
resolve_target()/split_into_reply_segments() actually touch.

Run with:
    pytest tests/test_quote_reply.py
"""
from __future__ import annotations

from dataclasses import dataclass

from TinyCTX.bridges.discord.quote_reply import (
    resolve_target,
    split_into_reply_segments,
)


@dataclass
class _FakeMessage:
    content: str


def test_no_quote_returns_single_plain_segment():
    segments = split_into_reply_segments("just a normal reply", candidates=[])
    assert len(segments) == 1
    assert segments[0].text == "just a normal reply"
    assert segments[0].target is None


def test_blank_text_returns_no_segments():
    assert split_into_reply_segments("   \n  ", candidates=[]) == []


def test_mid_line_gt_is_not_a_quote_marker():
    # '>' only counts as a quote marker at the START of a line -- this must
    # not be mistaken for a quote block or break the regex.
    text = "hellisir>dhwnfb>dawdw"
    segments = split_into_reply_segments(text, candidates=[])
    assert len(segments) == 1
    assert segments[0].text == text
    assert segments[0].target is None


def test_single_quote_block_binds_forward():
    candidates = [_FakeMessage(content="hi there, how are you?")]
    text = "> hi there\ngot it, thanks!"
    segments = split_into_reply_segments(text, candidates)

    assert len(segments) == 1
    assert segments[0].text == "got it, thanks!"
    assert segments[0].target is candidates[0]


def test_lead_in_text_before_first_quote_is_plain_segment():
    candidates = [_FakeMessage(content="hi there")]
    text = "Sure, on that first point:\n> hi there\ngot it, thanks!"
    segments = split_into_reply_segments(text, candidates)

    assert len(segments) == 2
    assert segments[0].text == "Sure, on that first point:"
    assert segments[0].target is None
    assert segments[1].text == "got it, thanks!"
    assert segments[1].target is candidates[0]


def test_multiple_quote_blocks_split_into_multiple_segments():
    msg1 = _FakeMessage(content="hi there, nice to meet you")
    msg2 = _FakeMessage(content="what about tomorrow's meeting")
    candidates = [msg2, msg1]  # most-recent-first, like discord history()

    text = (
        "Sure, on that first point:\n"
        "> hi there\n"
        "got it, thanks!\n"
        "> what about tomorrow\n"
        "let me check on that"
    )
    segments = split_into_reply_segments(text, candidates)

    assert [s.text for s in segments] == [
        "Sure, on that first point:",
        "got it, thanks!",
        "let me check on that",
    ]
    assert segments[0].target is None
    assert segments[1].target is msg1
    assert segments[2].target is msg2


def test_multiline_quote_block_is_one_block():
    candidates = [_FakeMessage(content="line one\nline two")]
    text = "> line one\n> line two\nokay noted"
    segments = split_into_reply_segments(text, candidates)

    assert len(segments) == 1
    assert segments[0].text == "okay noted"
    assert segments[0].target is candidates[0]


def test_trailing_quote_with_no_following_prose_produces_no_segment():
    candidates = [_FakeMessage(content="what about tomorrow")]
    text = "got it, thanks!\n> what about tomorrow"
    segments = split_into_reply_segments(text, candidates)

    # Lead-in segment only; the trailing quote has nothing after it to send.
    assert len(segments) == 1
    assert segments[0].text == "got it, thanks!"
    assert segments[0].target is None


def test_unmatched_quote_kept_verbatim_not_guessed():
    candidates = [_FakeMessage(content="totally unrelated message")]
    text = "> something nobody said\nhere's my reply"
    segments = split_into_reply_segments(text, candidates)

    # No confident match -- keep the quote block verbatim rather than
    # silently stripping content the agent wrote, and don't guess a target.
    assert len(segments) == 1
    assert segments[0].text == "> something nobody said\nhere's my reply"
    assert segments[0].target is None


def test_matched_quote_is_stripped_from_output():
    candidates = [_FakeMessage(content="hi there, nice to meet you")]
    text = "> hi there\ngot it, thanks!"
    segments = split_into_reply_segments(text, candidates)

    assert len(segments) == 1
    assert segments[0].text == "got it, thanks!"
    assert "hi there" not in segments[0].text
    assert segments[0].target is candidates[0]


def test_unmatched_trailing_quote_with_no_prose_still_sent_verbatim():
    candidates = [_FakeMessage(content="totally unrelated message")]
    text = "got it, thanks!\n> something nobody said"
    segments = split_into_reply_segments(text, candidates)

    # Unlike the matched case, an unresolved trailing quote isn't dropped --
    # its raw text has nowhere to go but its own segment.
    assert len(segments) == 2
    assert segments[0].text == "got it, thanks!"
    assert segments[0].target is None
    assert segments[1].text == "> something nobody said"
    assert segments[1].target is None


def test_short_quote_below_min_len_is_not_matched():
    candidates = [_FakeMessage(content="yes")]
    text = "> yes\nokay cool"
    segments = split_into_reply_segments(text, candidates, min_len=8)

    assert segments[0].target is None


def test_resolve_target_prefers_most_recent_on_tie():
    older = _FakeMessage(content="hello world how are you")
    newer = _FakeMessage(content="hello world how are you")
    # most-recent-first ordering, as discord.py's history() yields
    target = resolve_target("hello world how are you", [newer, older])
    assert target is newer


def test_resolve_target_substring_either_direction():
    # Quote is a truncated prefix of the real message.
    msg = _FakeMessage(content="hello world how are you doing today")
    assert resolve_target("hello world how are you", [msg]) is msg

    # Quote is longer than / padded relative to the real message content.
    msg2 = _FakeMessage(content="hello world")
    assert resolve_target("well, hello world, nice to see you", [msg2]) is msg2


def test_no_candidates_never_matches():
    assert resolve_target("hello world how are you", []) is None


def test_matches_across_humanized_vs_raw_mention_spelling():
    # Real regression: the agent's context shows humanized "@username" for
    # a mention (bridge.py humanizes inbound text before the agent sees
    # it), so it echoes "@username" back when quoting -- but the actual
    # channel-history message still has the raw Discord "<@snowflake>"
    # mention. The mention spelling differs; the surrounding sentence
    # should still match.
    real_message = _FakeMessage(
        content='hey <@1462978477119508530> try ">" quote replying to me. '
                'you know what that is, right'
    )
    quote_content = ('hey @Yumeko try ">" quote replying to me. '
                      'you know what that is, right')
    assert resolve_target(quote_content, [real_message]) is real_message

    text = f"> {quote_content}\n<@845336457153740830> oh, you mean like this?"
    segments = split_into_reply_segments(text, [real_message])
    assert len(segments) == 1
    assert segments[0].target is real_message
    assert segments[0].text == "<@845336457153740830> oh, you mean like this?"

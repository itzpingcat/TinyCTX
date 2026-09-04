"""
tests/test_label_prefix_strip.py

context.py assemble() injects "【{author_id}】: " on USER turns only (see
context.py ~line 744) to attribute speakers in multi-participant chats.
Models occasionally imitate the pattern in-context and start echoing
"【SomeName】: " at the head of their own replies, which then reinforces
itself turn over turn once it lands in stored history.

TinyCTX.modules.ctx_tools's _LabelPrefixStripHook implements AgentCycle's
stream_text_hooks protocol (reset/process/flush -- see agent.py __init__)
to strip a leading "【label】: " from the live stream before any
AgentTextChunk reaches a client, regardless of how the provider splits it
across TextDelta chunks. Covers the hook in isolation and wired through
AgentCycle._stream_inference end-to-end.

Run with:
    pytest tests/test_label_prefix_strip.py
"""
from __future__ import annotations

import asyncio

import pytest

from TinyCTX.agent import AgentCycle
from TinyCTX.ai import TextDelta, LLMError
from TinyCTX.contracts import AgentTextChunk
from TinyCTX.modules.ctx_tools.__main__ import _LabelPrefixStripHook


CASES = [
    # (deltas, expected released text)
    (["【", "Yu", "meko", "】", ":", " Hello", " there!"], "Hello there!"),
    (["【Yumeko】", ": Hello there!"], "Hello there!"),
    (["】: Hello"], "】: Hello"),                     # no opening 【, literal text kept
    (["【Yumeko】:"], ""),                             # prefix only, no body
    (["Hello", " there, no prefix"], "Hello there, no prefix"),
    (["【Yumeko】: Hi"], "Hi"),
    (["Hi"], "Hi"),
    (["No leading bracket but contains 【weird】 mid-text"],
     "No leading bracket but contains 【weird】 mid-text"),
    (["a" * 50], "a" * 50),                            # well past the buffer cap, no bracket
    (["line1\n", "【Yumeko】: fake"], "line1\n【Yumeko】: fake"),  # 【 not at true start
    (["【", "】", ":", "x"], "x"),                     # empty label
    (["【Yumeko】:", " ", " ", "Hi"], "Hi"),            # separator space split across deltas
    (["【Yumeko】: "], ""),                             # prefix + trailing space, stream ends there
]


# ---------------------------------------------------------------------------
# Hook in isolation
# ---------------------------------------------------------------------------

def _drive(hook, deltas):
    hook.reset()
    out = []
    for d in deltas:
        released = hook.process(d)
        if released:
            out.append(released)
    tail = hook.flush()
    if tail:
        out.append(tail)
    return "".join(out)


@pytest.mark.parametrize("deltas,expected", CASES)
def test_hook_strips_label_prefix(deltas, expected):
    hook = _LabelPrefixStripHook(max_buffer=40)
    assert _drive(hook, deltas) == expected


def test_hook_reset_clears_state_between_turns():
    hook = _LabelPrefixStripHook(max_buffer=40)
    assert _drive(hook, ["【Yumeko】: first turn"]) == "first turn"
    # A second turn must not see leftover state from the first.
    assert _drive(hook, ["【Yumeko】: second turn"]) == "second turn"


def test_hook_passthrough_once_resolved_is_cheap_and_unmodified():
    hook = _LabelPrefixStripHook(max_buffer=40)
    hook.reset()
    assert hook.process("Hi") == "Hi"          # resolves immediately, no bracket
    assert hook.process(" there") == " there"  # straight passthrough, no re-checking
    assert hook.flush() == ""


# ---------------------------------------------------------------------------
# Wired through AgentCycle._stream_inference (integration)
# ---------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, deltas):
        self._deltas = deltas

    async def stream(self, messages, tools=None, priority=0):
        for d in self._deltas:
            yield d


def _make_cycle(deltas, with_hook=True):
    cycle = AgentCycle.__new__(AgentCycle)  # bypass __init__
    cycle.models = {"main": _FakeLLM(deltas)}
    cycle.stream_text_hooks = [_LabelPrefixStripHook(max_buffer=40)] if with_hook else []
    return cycle


async def _run(deltas, with_hook=True):
    cycle = _make_cycle(deltas, with_hook=with_hook)
    meta = {"tail_node_id": "t", "trace_id": "tr", "reply_to_message_id": None}
    abort = asyncio.Event()
    chunks_out = []
    result = None
    async for ev in cycle._stream_inference(
        messages=[], tools=None, model_chain=["main"], abort_event=abort, meta=meta
    ):
        if isinstance(ev, tuple):
            result = ev
        elif isinstance(ev, AgentTextChunk):
            chunks_out.append(ev.text)
    return "".join(chunks_out), result


def test_real_injected_prefix_shape_from_context_py():
    # Mirrors context.py's actual f"【{label}】: " construction for a
    # realistic author_id, streamed as small multi-char deltas (closer to
    # real provider token granularity than character-by-character).
    label = "walnutseal1"
    full = f"【{label}】: Here is the actual reply."
    deltas = [TextDelta(text=full[i:i + 4]) for i in range(0, len(full), 4)]
    streamed, result = asyncio.run(_run(deltas))
    assert streamed == "Here is the actual reply."
    hist_chunks, calls, error = result
    assert error is None
    assert "".join(hist_chunks) == "Here is the actual reply."


def test_no_hooks_registered_streams_raw_text_unmodified():
    # With no hooks wired (e.g. a module that never registered one), the
    # mechanism in agent.py must be a pure passthrough.
    deltas = [TextDelta(text="【Yumeko】: still here")]
    streamed, result = asyncio.run(_run(deltas, with_hook=False))
    assert streamed == "【Yumeko】: still here"


def test_llm_error_after_partial_buffer_still_streams_buffer_before_failing():
    # An error mid-stream, before the prefix buffer resolved, must not
    # silently swallow buffered text on the LIVE stream (even though the
    # turn errors out overall) -- covers the flush-on-stream-end path.
    #
    # NOTE: the sentinel tuple's `chunks` list is discarded on the
    # all-models-failed exit path regardless of this hook (pre-existing,
    # unrelated behavior in agent.py: `yield ([], [], error or "all models
    # failed")` always returns an empty chunks list on error, even with
    # real partial text) -- not asserted here, flagged separately.
    deltas = [TextDelta(text="Hel"), LLMError(message="boom")]
    streamed, result = asyncio.run(_run(deltas))
    assert streamed == "Hel"
    hist_chunks, calls, error = result
    assert error == "boom"

"""
tests/test_librarian_common.py

Regression test: modules/memory/librarian_common.agent_loop's manual
tool-calling loop must forward reasoning (ThinkingDelta) back to the model
as a real reasoning_content field on the assistant message it appends to
`messages`, the same wire convention context.py's _render() uses for the
main AgentCycle. Before this fix, agent_loop only collected TextDelta and
silently dropped all thinking every cycle.
"""
from __future__ import annotations

import pytest

from TinyCTX.ai import TextDelta, ThinkingDelta, ToolCallAssembled
from TinyCTX.modules.memory.librarian_common import agent_loop


class _FakeLLM:
    """Replays one scripted list of events per call to .stream(), in order."""

    def __init__(self, cycles: list[list]):
        self._cycles = cycles
        self.calls: list[list[dict]] = []

    async def stream(self, messages, tools=None, priority=0):
        # Record a deep-enough snapshot to inspect what was actually sent.
        self.calls.append([dict(m) for m in messages])
        cycle = self._cycles[len(self.calls) - 1]
        for ev in cycle:
            yield ev


class _FakeHandler:
    def get_tool_definitions(self):
        return []

    async def execute_tool_call(self, call, caller):
        return {"success": True, "result": "ok"}


class _FakeLogger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_reasoning_content_forwarded_to_next_cycle():
    llm = _FakeLLM([
        # cycle 0: thinks, then calls a tool
        [
            ThinkingDelta(text="pondering the graph"),
            TextDelta(text="I'll look this up."),
            ToolCallAssembled(call_id="c1", tool_name="search_memory", args={}),
        ],
        # cycle 1: no tool calls -> loop returns
        [TextDelta(text="done")],
    ])
    await agent_loop(llm, "system prompt", "user prompt", _FakeHandler(), _FakeLogger())

    assert len(llm.calls) == 2
    second_call_messages = llm.calls[1]
    assistant_msgs = [m for m in second_call_messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].get("reasoning_content") == "pondering the graph"
    assert assistant_msgs[0]["content"] == "I'll look this up."


@pytest.mark.asyncio
async def test_no_reasoning_content_key_when_model_did_not_think():
    llm = _FakeLLM([
        [
            TextDelta(text="just acting, no thinking"),
            ToolCallAssembled(call_id="c1", tool_name="search_memory", args={}),
        ],
        [TextDelta(text="done")],
    ])
    await agent_loop(llm, "system prompt", "user prompt", _FakeHandler(), _FakeLogger())

    second_call_messages = llm.calls[1]
    assistant_msgs = [m for m in second_call_messages if m["role"] == "assistant"]
    assert "reasoning_content" not in assistant_msgs[0]

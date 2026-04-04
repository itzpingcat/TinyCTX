from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai import TextDelta
from db import ConversationDB
from modules.subagents import __main__ as subagents_module
from subagents import reset_subagent_tasks


class _MockConfig:
    def __init__(self, ws_path: str, extra: dict | None = None):
        self.workspace = type("WS", (), {"path": ws_path})()
        self.extra = extra or {}


class _MockToolHandler:
    def __init__(self):
        self.tools: dict[str, object] = {}
        self.always_on: dict[str, bool] = {}

    def register_tool(self, func, always_on=False):
        self.tools[func.__name__] = func
        self.always_on[func.__name__] = always_on


class _MockContext:
    def __init__(self):
        self.prompts: dict[str, tuple[object, object]] = {}

    def register_prompt(self, pid, provider, *, role="system", priority=0):
        self.prompts[pid] = (
            type("PromptSlot", (), {"pid": pid, "role": role, "priority": priority})(),
            provider,
        )


class _MockAgent:
    def __init__(self, ws_path: str, *, extra: dict | None = None, is_subagent: bool = False):
        self.config = _MockConfig(ws_path, extra=extra)
        self.tool_handler = _MockToolHandler()
        self.context = _MockContext()
        self.is_subagent = is_subagent


def _make_config(tmp_path, *, extra: dict | None = None):
    cfg = MagicMock()
    primary_mc = MagicMock()
    primary_mc.is_embedding = False
    primary_mc.tokens_per_image = 280
    cfg.models = {"primary": primary_mc}
    cfg.llm.primary = "primary"
    cfg.llm.fallback = []
    cfg.llm.fallback_on.any_error = False
    cfg.llm.fallback_on.http_codes = [429, 500]
    cfg.context = 4096
    cfg.max_tool_cycles = 5
    cfg.workspace.path = str(tmp_path)
    cfg.attachments = MagicMock()
    cfg.extra = extra or {}
    cfg.get_model_config = MagicMock(return_value=MagicMock(vision=False, supports_vision=False))
    return cfg


def _make_cursor(tmp_path) -> str:
    db = ConversationDB(tmp_path / "agent.db")
    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content="session:test")
    return node.id


def _text_stream(*texts):
    async def _gen(messages, tools=None):
        for text in texts:
            yield TextDelta(text=text)
    return _gen


@pytest.fixture(autouse=True)
def _cleanup_subagents():
    reset_subagent_tasks()
    yield
    reset_subagent_tasks()


def test_register_exposes_spawn_and_wait_as_always_on(tmp_path: Path):
    agent = _MockAgent(str(tmp_path))

    subagents_module.register(agent)

    assert "spawn_agent" in agent.tool_handler.tools
    assert "wait_agent" in agent.tool_handler.tools
    assert agent.tool_handler.always_on["spawn_agent"] is True
    assert agent.tool_handler.always_on["wait_agent"] is True

    slot, provider = agent.context.prompts["subagents"]
    prompt = provider(None)
    assert slot.role == "system"
    assert slot.priority == 13
    assert "Use spawn_agent for bounded side tasks" in prompt


def test_register_skips_subagent_agents(tmp_path: Path):
    agent = _MockAgent(str(tmp_path), is_subagent=True)

    subagents_module.register(agent)

    assert agent.tool_handler.tools == {}
    assert agent.context.prompts == {}


@pytest.mark.asyncio
async def test_spawn_agent_and_wait_agent_complete_on_child_branch(tmp_path: Path):
    from agent import AgentLoop

    cfg = _make_config(tmp_path)
    node_id = _make_cursor(tmp_path)
    llm = MagicMock()
    llm.stream = _text_stream("subagent finished cleanly")

    with patch("agent.MODULES_DIR", Path("/nonexistent")):
        with patch("agent._build_llm", return_value=llm):
            agent = AgentLoop(tail_node_id=node_id, config=cfg)
            subagents_module.register(agent)
            spawn_agent = agent.tool_handler.tools["spawn_agent"]["function"]
            wait_agent = agent.tool_handler.tools["wait_agent"]["function"]

            spawned = json.loads(await spawn_agent("Summarize the task."))
            waited = json.loads(
                await wait_agent(
                    spawned["task_id"],
                    timeout_seconds=1.0,
                )
            )

    assert spawned["status"] == "running"
    assert waited["status"] == "completed"
    assert waited["result"] == "subagent finished cleanly"
    assert waited["final_tail_node_id"] is not None

    db = ConversationDB(tmp_path / "agent.db")
    ancestors = db.get_ancestors(waited["final_tail_node_id"])
    assert any(node.role == "user" and node.content == "Summarize the task." for node in ancestors)
    assert any(
        node.role == "assistant" and "subagent finished cleanly" in node.content
        for node in ancestors
    )


@pytest.mark.asyncio
async def test_wait_agent_can_poll_running_task(tmp_path: Path):
    from agent import AgentLoop

    cfg = _make_config(tmp_path)
    node_id = _make_cursor(tmp_path)

    async def slow_stream(messages, tools=None):
        await asyncio.sleep(0.05)
        yield TextDelta(text="slow completion")

    llm = MagicMock()
    llm.stream = slow_stream

    with patch("agent.MODULES_DIR", Path("/nonexistent")):
        with patch("agent._build_llm", return_value=llm):
            agent = AgentLoop(tail_node_id=node_id, config=cfg)
            subagents_module.register(agent)
            spawn_agent = agent.tool_handler.tools["spawn_agent"]["function"]
            wait_agent = agent.tool_handler.tools["wait_agent"]["function"]

            spawned = json.loads(await spawn_agent("Do the slow thing."))
            polled = json.loads(
                await wait_agent(
                    spawned["task_id"],
                    timeout_seconds=0.0,
                )
            )
            completed = json.loads(
                await wait_agent(
                    spawned["task_id"],
                    timeout_seconds=1.0,
                )
            )

    assert polled["status"] == "running"
    assert "still running" in polled["message"].lower()
    assert completed["status"] == "completed"
    assert completed["result"] == "slow completion"


@pytest.mark.asyncio
async def test_wait_agent_is_scoped_to_own_agent_registry(tmp_path: Path):
    from agent import AgentLoop

    cfg = _make_config(tmp_path)
    first_node_id = _make_cursor(tmp_path)
    second_node_id = _make_cursor(tmp_path)
    llm = MagicMock()
    llm.stream = _text_stream("session scoped")

    with patch("agent.MODULES_DIR", Path("/nonexistent")):
        with patch("agent._build_llm", return_value=llm):
            first_agent = AgentLoop(tail_node_id=first_node_id, config=cfg)
            second_agent = AgentLoop(tail_node_id=second_node_id, config=cfg)
            subagents_module.register(first_agent)
            subagents_module.register(second_agent)
            first_spawn = first_agent.tool_handler.tools["spawn_agent"]["function"]
            first_wait = first_agent.tool_handler.tools["wait_agent"]["function"]
            second_wait = second_agent.tool_handler.tools["wait_agent"]["function"]

            spawned = json.loads(await first_spawn("Scoped task."))
            missing = json.loads(await second_wait(spawned["task_id"], timeout_seconds=0.0))
            completed = json.loads(await first_wait(spawned["task_id"], timeout_seconds=1.0))

    assert missing["status"] == "missing"
    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_spawn_agent_enforces_max_concurrent_limit(tmp_path: Path):
    from agent import AgentLoop

    cfg = _make_config(
        tmp_path,
        extra={"subagents": {"max_concurrent": 1}},
    )
    node_id = _make_cursor(tmp_path)

    async def slow_stream(messages, tools=None):
        await asyncio.sleep(0.05)
        yield TextDelta(text="slow completion")

    llm = MagicMock()
    llm.stream = slow_stream

    with patch("agent.MODULES_DIR", Path("/nonexistent")):
        with patch("agent._build_llm", return_value=llm):
            agent = AgentLoop(tail_node_id=node_id, config=cfg)
            subagents_module.register(agent)
            spawn_agent = agent.tool_handler.tools["spawn_agent"]["function"]
            wait_agent = agent.tool_handler.tools["wait_agent"]["function"]

            first = json.loads(await spawn_agent("First task."))
            second = json.loads(await spawn_agent("Second task."))
            await wait_agent(first["task_id"], timeout_seconds=1.0)

    assert first["status"] == "running"
    assert second["status"] == "error"
    assert "Too many subagents" in second["error"]


@pytest.mark.asyncio
async def test_spawn_agent_prunes_completed_tasks_before_new_spawn(tmp_path: Path):
    from agent import AgentLoop

    cfg = _make_config(
        tmp_path,
        extra={"subagents": {"completed_ttl_seconds": 0}},
    )
    node_id = _make_cursor(tmp_path)
    llm = MagicMock()
    llm.stream = _text_stream("done")

    with patch("agent.MODULES_DIR", Path("/nonexistent")):
        with patch("agent._build_llm", return_value=llm):
            agent = AgentLoop(tail_node_id=node_id, config=cfg)
            subagents_module.register(agent)
            spawn_agent = agent.tool_handler.tools["spawn_agent"]["function"]
            wait_agent = agent.tool_handler.tools["wait_agent"]["function"]

            first = json.loads(await spawn_agent("First task."))
            await wait_agent(first["task_id"], timeout_seconds=1.0)
            second = json.loads(await spawn_agent("Second task."))

    assert second["status"] == "running"
    assert second["pruned_completed_tasks"] == 1

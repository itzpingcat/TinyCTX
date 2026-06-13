"""
modules/memory/librarian_agents.py

Pure agent logic for the knowledge librarian: buffer ingestion and targeted
edits. Deduplication logic lives in dedup_agents.py.

Called by LibrarianRunner (_poll_cycle) in __main__.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


# Re-export run_dedup_cycle so callers that import it from here still work.
from TinyCTX.modules.memory.dedup_agents import run_dedup_cycle as run_dedup_cycle  # noqa: E402


async def get_relation_types(conn) -> str:
    """Return a comma-separated string of relation types: defaults union live graph labels."""
    defaults = [
        t.strip()
        for line in (_PROMPTS_DIR / "default_relation_types.txt").read_text(encoding="utf-8").splitlines()
        for t in line.split(",")
        if t.strip()
    ]
    r = await conn.execute(
        "MATCH ()-[r:Relation]->() WHERE r.superseded_at IS NULL RETURN DISTINCT r.relation ORDER BY r.relation"
    )
    live = []
    while r.has_next():
        live.append(r.get_next()[0])
    extras = [l for l in live if l not in defaults]
    return ", ".join(defaults + extras)


async def _aset(conn, uid: str, field: str, value):
    """Async single-field SET."""
    return await conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


# ---------------------------------------------------------------------------
# Conversation node -> text
# ---------------------------------------------------------------------------

def nodes_to_text(conv_db, node_ids: list[str], batch_size: int) -> tuple[str, str]:
    """
    Render up to batch_size nodes as '[author]: content' lines.
    Returns (batch_text, agent_name) where agent_name is the last assistant
    author_id seen in the batch, or 'assistant' if none found.
    """
    lines: list[str] = []
    agent_name = "assistant"
    for node_id in node_ids[:batch_size]:
        node = conv_db.get_node(node_id)
        if node is None or node.role not in ("user", "assistant"):
            continue
        author = node.author_id or node.role
        if node.role == "assistant" and node.author_id:
            agent_name = node.author_id
        content = node.content or ""
        if content.startswith("["):
            try:
                blocks = json.loads(content)
                texts  = [b.get("text", "") for b in blocks
                          if isinstance(b, dict) and b.get("type") == "text"]
                content = " ".join(texts)
            except Exception:
                pass
        content = content.strip()
        if content:
            lines.append(f"[{author}]: {content}")
    return "\n".join(lines), agent_name


# ---------------------------------------------------------------------------
# Shared: build a ToolCallHandler for librarian agents
# ---------------------------------------------------------------------------

def _make_tool_handler():
    from TinyCTX.utils.tool_handler import ToolCallHandler
    import TinyCTX.modules.memory.tools as tools

    handler = ToolCallHandler()
    for fn in [
        tools.kg_search,
        tools.kg_add_entity,
        tools.kg_update_entity,
        tools.kg_merge_entities,
        tools.kg_add_relationship,
        tools.kg_delete_entity,
        tools.kg_delete_relationship,
        tools.kg_get_entity,
    ]:
        handler.register_tool(fn, always_on=True, min_permission=0)
    return handler


# ---------------------------------------------------------------------------
# Buffer agent
# ---------------------------------------------------------------------------

async def run_buffer_agent(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    batch_text: str,
    agent_name: str,
    agent_logger: logging.Logger,
) -> None:
    """Ingest a batch of conversation nodes into the knowledge graph."""
    relation_vocab = await get_relation_types(conn)
    await _agent_loop(
        llm,
        _prompt("buffer_system.txt").format(
            relation_vocab=relation_vocab,
            agent_name=agent_name,
        ),
        _prompt("buffer_user.txt").format(batch_text=batch_text),
        _make_tool_handler(),
        agent_logger,
    )


# ---------------------------------------------------------------------------
# Targeted agent
# ---------------------------------------------------------------------------

async def run_targeted_agent(
    cfg: dict,
    conn,
    write_lock: asyncio.Lock,
    llm,
    prompt: str,
    agent_logger: logging.Logger,
) -> None:
    """Execute a specific graph-edit instruction."""
    relation_vocab = await get_relation_types(conn)
    await _agent_loop(
        llm,
        _prompt("targeted_system.txt").format(relation_vocab=relation_vocab),
        prompt,
        _make_tool_handler(),
        agent_logger,
    )


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def _agent_loop(
    llm,
    system_prompt: str,
    user_prompt: str,
    handler,
    agent_logger: logging.Logger,
    max_cycles: int = 20,
) -> None:
    from TinyCTX.ai import TextDelta, ToolCallAssembled, LLMError

    class _InternalCaller:
        permission_level = 25
        username = "librarian"

    tool_defs = handler.get_tool_definitions(caller_level=25)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    for cycle in range(max_cycles):
        text_chunks: list[str] = []
        tool_calls:  list[dict] = []

        async for event in llm.stream(messages, tools=tool_defs):
            if isinstance(event, TextDelta):
                text_chunks.append(event.text)
            elif isinstance(event, ToolCallAssembled):
                tool_calls.append({"id": event.call_id, "name": event.tool_name, "args": event.args})
            elif isinstance(event, LLMError):
                logger.error("[memory/librarian] LLM error: %s", event.message)
                return

        response_text = "".join(text_chunks)
        if response_text:
            label = "[final]" if not tool_calls else f"[cycle {cycle}]"
            agent_logger.info("%s %s", label, response_text)

        if not tool_calls:
            return

        messages.append({
            "role":    "assistant",
            "content": response_text,
            "tool_calls": [
                {
                    "id":   tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            outcome = await handler.execute_tool_call(
                {"id": tc["id"], "function": {"name": tc["name"], "arguments": tc["args"]}},
                _InternalCaller(),
            )
            result = outcome["result"] if outcome["success"] else outcome["error"]
            agent_logger.debug("  tool %s -> %s", tc["name"], result)
            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      str(result),
            })

    logger.warning("[memory/librarian] hit max_cycles (%d)", max_cycles)

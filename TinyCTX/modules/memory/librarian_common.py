"""
modules/memory/librarian_common.py

Shared plumbing for the librarian subagents (extractor / reviewer / deduper):
the tool handler wiring, the manual tool-calling agent loop, and the
injection-safe conversation-to-text renderer.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def make_tool_handler():
    """A ToolCallHandler exposing the FULL memory toolset to librarians."""
    from TinyCTX.utils.tool_handler import ToolCallHandler
    import TinyCTX.modules.memory.tools as tools

    handler = ToolCallHandler()
    for fn in [
        tools.search_memory,
        tools.memory_add_entity,
        tools.memory_update_entity_description,
        tools.memory_set_entity_pinned,
        tools.memory_set_entity_scope,
        tools.memory_delete_entity,
        tools.memory_set_relationship,
        tools.memory_delete_relationship,
        tools.memory_merge_into,
        tools.memory_stats,
    ]:
        handler.register_tool(fn, always_on=True, min_permission=0)
    return handler


def nodes_to_text(conv_db, node_ids: list[str], batch_size: int) -> tuple[str, str]:
    """
    Render up to batch_size conversation nodes as '【author】: content' lines
    (fullwidth brackets, matching context.py). Content is passed through
    sanitize_brackets() so it cannot forge the delimiter (injection defense).
    Returns (text, agent_name).
    """
    from TinyCTX.utils.sanitize import sanitize_brackets

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
                content = " ".join(
                    b.get("text", "") for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            except Exception:
                pass
        content = sanitize_brackets(content.strip())
        if content:
            lines.append(f"【{author}】: {content}")
    return "\n".join(lines), agent_name


async def agent_loop(llm, system_prompt: str, user_prompt: str, handler, agent_logger,
                     max_cycles: int = 20) -> None:
    """Manual tool-calling loop. Caller is responsible for having bound the
    scope contextvar (tools.scope_context) before invoking this."""
    from TinyCTX.ai import TextDelta, ToolCallAssembled, LLMError

    class _InternalCaller:
        permission_level = 25
        username = "librarian"

    tool_defs = handler.get_tool_definitions(caller_level=25)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    for cycle in range(max_cycles):
        text_chunks: list[str] = []
        tool_calls: list[dict] = []
        async for event in llm.stream(messages, tools=tool_defs, priority=15):
            if isinstance(event, TextDelta):
                text_chunks.append(event.text)
            elif isinstance(event, ToolCallAssembled):
                tool_calls.append({"id": event.call_id, "name": event.tool_name, "args": event.args})
            elif isinstance(event, LLMError):
                logger.error("[memory/librarian] LLM error: %s", event.message)
                return

        response_text = "".join(text_chunks)
        if response_text:
            agent_logger.info("%s %s", "[final]" if not tool_calls else f"[cycle {cycle}]", response_text)
        if not tool_calls:
            return

        messages.append({
            "role": "assistant",
            "content": response_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
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
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})

    logger.warning("[memory/librarian] hit max_cycles (%d)", max_cycles)

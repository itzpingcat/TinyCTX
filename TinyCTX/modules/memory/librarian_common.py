"""
modules/memory/librarian_common.py

Shared plumbing for the librarian subagents (extractor / reviewer / deduper):
the tool handler wiring, the manual tool-calling agent loop, and the
injection-safe conversation-to-text renderer.
"""
from __future__ import annotations

import json
import logging

from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)

# Tools that only read the graph vs. tools that mutate it — the librarian's
# internal caller (_InternalCaller below) holds both MEMORY_READ and
# MEMORY_WRITE, so this split doesn't change what the librarian itself can
# do; it keeps each tool's declared required_permissions honest rather than
# gating every tool — reads and writes alike — behind a single flat bool.
_READ_TOOLS = frozenset({"search_memory", "memory_stats"})


def make_tool_handler():
    """A ToolCallHandler exposing the FULL memory toolset to librarians."""
    from TinyCTX.tool_handling import ToolCallHandler
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
        perm = Permission.MEMORY_READ if fn.__name__ in _READ_TOOLS else Permission.MEMORY_WRITE
        handler.register_tool(fn, always_on=True, required_permissions={perm})
    return handler


def _render_node(conv_db, node_id: str):
    """Return (author, content) for a node, or None if not renderable."""
    from TinyCTX.utils.sanitize import sanitize_brackets, sanitize_special_tokens

    node = conv_db.get_node(node_id)
    if node is None or node.role not in ("user", "assistant"):
        return None
    author = node.author_id or node.role
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
    content = sanitize_special_tokens(sanitize_brackets(content.strip()))
    if not content:
        return None
    return author, content, (node.role == "assistant" and node.author_id)


def nodes_to_text(conv_db, node_ids: list[str], batch_size: int,
                  overlap_node_ids: list[str] | None = None) -> tuple[str, str]:
    """
    Render up to batch_size unread conversation nodes as '【author】: content'
    lines (fullwidth brackets, matching context.py), optionally preceded by
    overlap_node_ids — already-visited nodes rendered the same way but wrapped
    in an <already_extracted> block so the extractor has trailing context for
    small/fragmented new batches without re-extracting content from them.
    Content is passed through sanitize_brackets() so it cannot forge the
    delimiter, and sanitize_special_tokens() so it cannot forge fake turn
    boundaries with LLM special/control tokens (injection defense).
    Returns (text, agent_name).
    """
    agent_name = "assistant"

    def render(ids):
        nonlocal agent_name
        lines: list[str] = []
        for node_id in ids:
            rendered = _render_node(conv_db, node_id)
            if rendered is None:
                continue
            author, content, is_named_assistant = rendered
            if is_named_assistant:
                agent_name = author
            lines.append(f"【{author}】: {content}")
        return lines

    overlap_lines = render(overlap_node_ids) if overlap_node_ids else []
    new_lines = render(node_ids[:batch_size])

    parts: list[str] = []
    if overlap_lines:
        parts.append(
            "<already_extracted>\n"
            "The following is prior context, already ingested into memory. "
            "Do not extract from it again — use it only to correctly interpret "
            "what follows.\n" + "\n".join(overlap_lines) + "\n</already_extracted>"
        )
    parts.append("\n".join(new_lines))
    return "\n".join(p for p in parts if p.strip()), agent_name


async def agent_loop(llm, system_prompt: str, user_prompt: str, handler, agent_logger,
                     max_cycles: int = 40) -> None:
    """Manual tool-calling loop. Caller is responsible for having bound the
    scope contextvar (tools.scope_context) before invoking this."""
    from TinyCTX.ai import TextDelta, ToolCallAssembled, LLMError

    class _InternalCaller:
        """Synthetic caller for the librarian subagent — not a real user, so
        it isn't resolved against PermissionsConfig.template at all. Holds
        exactly the two bools make_tool_handler()'s registrations ever
        require (§ this module's docstring)."""
        username = "librarian"

        def effective_permissions(self, permissions_config=None) -> frozenset[Permission]:
            return frozenset({Permission.MEMORY_READ, Permission.MEMORY_WRITE})

    # minimal_tokens defaults False, so `caller` isn't consulted here — this
    # handler only ever holds the librarian's own dedicated toolset anyway.
    tool_defs = handler.get_tool_definitions()
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

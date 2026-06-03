"""
modules/present/__main__.py — Always-on present() tool.

Delivers workspace files directly to the user by appending an AgentOutboundFiles
event to agent.outbound_events, which the agent loop yields immediately after
the tool result — flowing through the normal reply_queue like any other event.
"""


def register_agent(agent) -> None:
    from pathlib import Path

    workspace = Path(agent.config.workspace.path).expanduser().resolve()

    async def present(media: list[str]) -> str:
        """Deliver files to the user.

        This is the ONLY way to deliver files to the user. Pass workspace-
        relative or absolute paths in `media`. Do NOT use read_file to send
        files — that only reads content for your own analysis.

        Args:
            media: List of file paths (workspace-relative or absolute) to
                   deliver to the user.
        """
        from TinyCTX.contracts import AgentOutboundFiles

        validated: list[str] = []
        for p in media:
            try:
                resolved = (workspace / p).resolve()
                resolved.relative_to(workspace)          # path traversal guard
            except ValueError:
                return f"Error: {p} is outside the workspace"
            if not resolved.is_file():
                return f"Error: {p} not found"
            validated.append(str(resolved))

        agent.outbound_events.append(AgentOutboundFiles(
            paths=tuple(validated),
            tail_node_id=agent.context.tail_node_id if agent.context else "",
            trace_id=agent.trace_id,
            reply_to_message_id="",
        ))

        names = ", ".join(Path(p).name for p in validated)
        return f"Successfully sent files: {names}"

    agent.tool_handler.register_tool(present, always_on=True, min_permission=25)

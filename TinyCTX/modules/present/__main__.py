"""
modules/present/__main__.py — Always-on present() tool.

Delivers workspace files directly to the user by firing an AgentOutboundFiles
event through the router's dispatch path. The agent receives a plain success
string as the tool result — no sentinel parsing, no structured return payload.
"""


def register(agent) -> None:
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

        event = AgentOutboundFiles(
            paths=tuple(validated),
            tail_node_id=agent.tail_node_id,
            lane_node_id=agent.lane_node_id,
            trace_id="present",
            reply_to_message_id="",
        )
        try:
            await agent.gateway._dispatch_event(event)
        except Exception as exc:
            return f"Error: failed to dispatch files — {exc}"

        names = ", ".join(Path(p).name for p in validated)
        return f"Successfully sent files: {names}"

    agent.tool_handler.register_tool(present, always_on=True)

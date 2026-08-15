"""
modules/present/__main__.py — Always-on present() tool.

Delivers workspace files directly to the user by appending an AgentOutboundFiles
event to agent.outbound_events, which the agent loop yields immediately after
the tool result — flowing through the normal reply_queue like any other event.

Security rules:
  1. All paths must resolve inside the workspace — no traversal.
  2. Core system files are blacklisted and silently dropped from batch calls,
     with a notice in the return value. A single-file call requesting exactly
     one system file can override this and send it directly, but doing so
     requires Permission.ROOT — checked dynamically, per docs/PERMISSIONS-PLAN.md
     §7.1, by the `required_permissions` classifier registered below (present
     is the third dynamic tool alongside `shell` and nothing else). By the
     time present()'s body runs, that check has already happened centrally in
     ToolCallHandler.execute_tool_call — present() itself no longer needs (or
     performs) a caller-level check.
"""

from TinyCTX.permissions import Permission


def _load_blacklist(module_dir) -> tuple[frozenset[str], frozenset[str]]:
    """Parse blacklist.txt into (file_names, dir_names), both lowercase.

    Lines ending with '/' are treated as directory prefixes.
    Blank lines and lines starting with '#' are ignored.
    """
    from pathlib import Path
    bl = Path(module_dir) / "blacklist.txt"
    file_names: set[str] = set()
    dir_names:  set[str] = set()
    if not bl.exists():
        return frozenset(), frozenset()
    for raw in bl.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("/"):
            dir_names.add(line.rstrip("/").lower())
        else:
            file_names.add(line.lower())
    return frozenset(file_names), frozenset(dir_names)


def _is_system_file(resolved, workspace, file_names, dir_names) -> bool:
    if resolved.name.lower() in file_names:
        return True
    try:
        rel = resolved.relative_to(workspace)
        if rel.parts and rel.parts[0].lower() in dir_names:
            return True
    except ValueError:
        pass
    return False


def register_agent(agent) -> None:
    from pathlib import Path

    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    file_names, dir_names = _load_blacklist(Path(__file__).parent)

    def _resolve_media_path(p: str) -> "Path | None":
        try:
            resolved = (workspace / p).resolve()
            resolved.relative_to(workspace)
        except ValueError:
            return None
        return resolved

    def _present_perms(media: list, **_ignored) -> set:
        """Dynamic classifier (§7.1): present always needs FILE_READ, plus
        ROOT when the call is a solo request for a core system file — the
        blacklist-override path. Mirrors present()'s own path resolution and
        blacklist check so the two never drift; on any resolution failure
        this stays conservative and does NOT add ROOT (the tool body will
        reject the path with its own error before delivering anything)."""
        needed = {Permission.FILE_READ}
        if isinstance(media, list) and len(media) == 1:
            resolved = _resolve_media_path(media[0])
            if resolved is not None and _is_system_file(resolved, workspace, file_names, dir_names):
                needed.add(Permission.ROOT)
        return needed

    async def present(media: list[str]) -> str:
        """Deliver files to the user.

        This is the ONLY way to deliver files to the user. Pass workspace-
        relative or absolute paths in `media`. Do NOT use read_file to send
        files — that only reads content for your own analysis.

        All paths must be inside the workspace. Core system files (SOUL.md,
        AGENTS.md, TOOLS.md, agent.db, users.db, memory/) are blocked from
        batch calls. If you are certain you need to send a single system file,
        call present() with exactly one path — this requires the ROOT capability.

        Args:
            media: List of file paths (workspace-relative or absolute) to
                   deliver to the user.
        """
        from TinyCTX.contracts import AgentOutboundFiles

        validated: list[str] = []
        system_blocked: list[str] = []

        for p in media:
            # --- 1. Must be inside workspace ---
            try:
                resolved = (workspace / p).resolve()
                resolved.relative_to(workspace)
            except ValueError:
                return f"Error: '{p}' is outside the workspace."

            if not resolved.is_file():
                return f"Error: '{p}' not found."

            # --- 2. System file blacklist ---
            if _is_system_file(resolved, workspace, file_names, dir_names):
                is_solo = len(media) == 1
                if is_solo:
                    # Solo system-file request — the required_permissions
                    # classifier below already required (and, by the time
                    # this body runs, ToolCallHandler already confirmed the
                    # caller holds) Permission.ROOT for exactly this case.
                    # Allow through.
                    pass
                else:
                    system_blocked.append(resolved.name)
                    continue

            validated.append(str(resolved))

        notices: list[str] = []

        if system_blocked:
            blocked_str = ", ".join(system_blocked)
            notices.append(
                f"Note: the following core system file(s) were not sent: {blocked_str}. "
                "If you are absolutely sure you want to send them, call present() "
                "with exactly one system file path at a time (requires the ROOT capability)."
            )

        if not validated:
            return "\n".join(notices) if notices else "Error: no valid files to send."

        agent.outbound_events.append(AgentOutboundFiles(
            paths=tuple(validated),
            tail_node_id=agent.context.tail_node_id if agent.context else "",
            trace_id=agent.trace_id,
            reply_to_message_id="",
        ))

        names = ", ".join(Path(p).name for p in validated)
        result = f"Successfully sent: {names}."
        if notices:
            result += "\n" + "\n".join(notices)
        return result

    # Dynamic classifier (§7.1) — third alongside `shell`. listing_permissions
    # is deliberately left unset: under minimal_tokens, present should still
    # be listed for any caller who can deliver ordinary files, and a static
    # hint here could only ever be FILE_READ anyway (ROOT is a rare escalation,
    # not present's baseline requirement).
    agent.tool_handler.register_tool(present, always_on=True, required_permissions=_present_perms)

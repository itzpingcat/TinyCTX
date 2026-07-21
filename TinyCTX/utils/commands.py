"""
utils/commands.py — Lightweight slash-command registry.

Modules register namespaced commands at register() time:

    registry.register("memory", "consolidate", _do_consolidate, help="Run memory consolidation now")
    registry.register("heartbeat", "run", _do_tick, help="Fire one heartbeat tick immediately")

Bridges dispatch before pushing to the router:

    handled = await registry.dispatch(text, context)
    if handled:
        return  # don't push to router

Command syntax parsed here:
    /namespace [subcommand] [args...]

    /heartbeat run        → namespace="heartbeat", sub="run", args=[]
    /memory consolidate   → namespace="memory",    sub="consolidate", args=[]
    /memory               → namespace="memory",    sub="",           args=[]

`context` is whatever the bridge wants to pass through to handlers — typically
a dict with keys like "console", "agent", "cursor", "gateway".  Handlers are
async callables:

    async def handler(args: list[str], context: dict) -> None: ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

Handler = Callable[[list[str], dict], Awaitable[None]]

# Param spec: (name, python_type, description)
ParamSpec = list[tuple[str, type, str]]


@dataclass
class _Entry:
    namespace: str
    sub:       str        # "" for bare /namespace
    handler:   Handler
    help:      str = ""
    params:    ParamSpec = field(default_factory=list)


class CommandRegistry:
    def __init__(self) -> None:
        self._entries: list[_Entry] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        namespace: str,
        sub: str,
        handler: Handler,
        *,
        help: str = "",
        params: ParamSpec | None = None,
    ) -> None:
        """
        Register a command handler.

        namespace   — the word after the leading slash, e.g. "memory"
        sub         — optional subcommand word, e.g. "consolidate".
                      Use "" to handle bare `/namespace` with no subcommand.
        handler     — async callable(args: list[str], context: dict) -> None
        help        — one-line description shown by /help
        params      — optional list of (name, type, description) tuples.
                      Bridges use this to build typed native commands (e.g.
                      Discord slash command parameters). Types should be
                      str or int. If omitted, the command takes no parameters
                      on native bridges.
        """
        namespace = namespace.lower().strip()
        sub       = sub.lower().strip()
        self._entries = [e for e in self._entries if not (e.namespace == namespace and e.sub == sub)]
        self._entries.append(_Entry(
            namespace=namespace,
            sub=sub,
            handler=handler,
            help=help,
            params=params or [],
        ))
        logger.debug(
            "[commands] registered /%s%s",
            namespace, f" {sub}" if sub else "",
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, text: str, context: dict) -> bool:
        """
        Try to dispatch text as a slash command.

        Returns True if the text was handled (bridge should not push to router).
        Returns False if it was not a registered command (or not a slash command).
        """
        text = text.strip()
        if not text.startswith("/"):
            return False

        parts = text[1:].split()
        if not parts:
            return False

        namespace = parts[0].lower()
        sub       = parts[1].lower() if len(parts) > 1 else ""
        args      = parts[2:] if len(parts) > 2 else []

        # Try exact namespace+sub match first, then bare namespace match.
        entry = self._find(namespace, sub)
        if entry is None and sub:
            # Retry: maybe the full text after /namespace is meant as args
            # (no subcommand registered for this word).
            entry = self._find(namespace, "")
            if entry is not None:
                args = parts[1:]  # shift sub back into args
            else:
                entry = None

        if entry is None:
            logger.debug("[commands] no handler for /%s %s", namespace, sub)
            return False

        try:
            await entry.handler(args, context)
        except Exception:
            logger.exception("[commands] handler for /%s %s raised", namespace, sub)
        else:
            self._record_command_introspection(namespace, sub, args, context)
        return True

    @staticmethod
    def _record_command_introspection(namespace: str, sub: str, args: list[str], context: dict) -> None:
        """
        command_introspection: append this command invocation to session
        state (key "command_introspection_log") so agent.py's AgentCycle.run()
        can surface it to the LLM on its next turn. /reset is excluded — a
        reset starts a fresh branch and there's nothing for the old branch's
        LLM to be told about. Best-effort: silently no-ops if the flag is
        off, or if this bridge's context doesn't carry what we need
        (runtime + a node_id/cursor to attach the note to).
        """
        if namespace == "reset":
            return
        runtime = context.get("runtime")
        if runtime is None:
            return
        config = getattr(runtime, "config", None)
        if not getattr(config, "command_introspection", False):
            return
        node_id = (context.get("node_id") or context.get("cursor") or "").strip()
        if not node_id:
            return
        cmd_str = f"/{namespace}" + (f" {sub}" if sub else "") + (" " + " ".join(args) if args else "")
        try:
            log = list(runtime.db.get_state(node_id, "command_introspection_log", []) or [])
            log.append(cmd_str.strip())
            runtime.db.set_state(node_id, "command_introspection_log", log)
        except Exception:
            logger.exception("[commands] command_introspection: failed to record %r", cmd_str)

    def _find(self, namespace: str, sub: str) -> _Entry | None:
        for e in self._entries:
            if e.namespace == namespace and e.sub == sub:
                return e
        return None

    # ------------------------------------------------------------------
    # Help listing (used by /help in bridges)
    # ------------------------------------------------------------------

    def list_commands(self) -> list[tuple[str, str]]:
        """Return [(command_str, help_text), ...] sorted alphabetically."""
        rows = []
        for e in self._entries:
            cmd = f"/{e.namespace}" + (f" {e.sub}" if e.sub else "")
            rows.append((cmd, e.help))
        return sorted(rows, key=lambda r: r[0])

    def entries(self) -> list[_Entry]:
        """Return all registered entries (for bridges that need full metadata)."""
        return list(self._entries)

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

from TinyCTX import permissions as _permissions
from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)

Handler = Callable[[list[str], dict], Awaitable[None]]

# Param spec: (name, python_type, description)
ParamSpec = list[tuple[str, type, str]]

# Sentinel distinguishing "required_permissions not passed at all" (a
# forgotten declaration) from "required_permissions=None passed explicitly"
# (a deliberately ungated command) — same reasoning as
# tool_handling/handler.py's _UNSET. See docs/PERMISSIONS-PLAN.md §9 and
# assert_permissions_declared() below.
_UNSET = object()


@dataclass
class _Entry:
    namespace: str
    sub:       str        # "" for bare /namespace
    handler:   Handler
    help:      str = ""
    params:    ParamSpec = field(default_factory=list)
    required_permissions: "frozenset[Permission] | None" = None
    _permissions_declared: bool = False


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
        required_permissions: "set[Permission] | frozenset[Permission] | None" = _UNSET,
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
        required_permissions — the capability set the caller must hold for
                      dispatch() to invoke this handler at all. Checked
                      before the handler runs, using the caller resolved
                      from `context` (see _resolve_caller below) — same
                      expand-the-requirement rule as
                      tool_handling.handler.ToolCallHandler (§9). Pass None
                      explicitly for a deliberately ungated command; passing
                      nothing at all is a forgotten declaration and trips
                      assert_permissions_declared().
        """
        namespace = namespace.lower().strip()
        sub       = sub.lower().strip()
        declared = required_permissions is not _UNSET
        perms = None if required_permissions is _UNSET else required_permissions
        if perms is not None:
            perms = frozenset(perms)
        self._entries = [e for e in self._entries if not (e.namespace == namespace and e.sub == sub)]
        self._entries.append(_Entry(
            namespace=namespace,
            sub=sub,
            handler=handler,
            help=help,
            params=params or [],
            required_permissions=perms,
            _permissions_declared=declared,
        ))
        logger.debug(
            "[commands] registered /%s%s",
            namespace, f" {sub}" if sub else "",
        )

    def assert_permissions_declared(self) -> None:
        """
        Startup assertion (docs/PERMISSIONS-PLAN.md §9, mirroring
        tool_handling.handler's): every registered command must have called
        register() with an explicit required_permissions — a set, or None
        for a deliberately ungated command. A command that never passed the
        argument at all is a bug, not an ungated command.
        """
        undeclared = sorted(
            f"/{e.namespace}" + (f" {e.sub}" if e.sub else "")
            for e in self._entries if not e._permissions_declared
        )
        if undeclared:
            raise RuntimeError(
                "[commands] command(s) registered without declaring "
                f"required_permissions (pass a set[Permission] or explicitly "
                f"None): {undeclared}"
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

        denial = self._check_permission(entry, context)
        if denial is not None:
            logger.info("[commands] denied /%s %s: %s", namespace, sub, denial)
            send = context.get("send")
            if callable(send):
                try:
                    await send(denial)
                except Exception:
                    logger.exception("[commands] failed to deliver permission denial for /%s %s", namespace, sub)
            return True  # handled (denied) — don't push to router

        try:
            await entry.handler(args, context)
        except Exception:
            logger.exception("[commands] handler for /%s %s raised", namespace, sub)
        else:
            self._record_command_introspection(namespace, sub, args, context)
        return True

    @staticmethod
    def _check_permission(entry: _Entry, context: dict) -> str | None:
        """
        Returns a denial message if `entry` is gated and the resolved caller
        is missing a required permission (or no caller could be resolved at
        all for a gated command); None if the call may proceed.
        """
        if entry.required_permissions is None:
            return None
        runtime = context.get("runtime")
        caller = CommandRegistry._resolve_caller(runtime, context)
        if caller is None:
            return "[PERMISSION DENIED] could not resolve caller for this command."
        permissions_config = getattr(getattr(runtime, "config", None), "permissions", None)
        if permissions_config is None:
            from TinyCTX.config import PermissionsConfig
            permissions_config = PermissionsConfig()
        effective = caller.effective_permissions(permissions_config)
        needed = _permissions.expand(entry.required_permissions)
        missing = {p for p in needed if p not in effective}
        if missing:
            return f"[PERMISSION DENIED] missing: {sorted(p.value for p in missing)}"
        return None

    @staticmethod
    def _record_command_introspection(
        namespace: str, sub: str, args: list[str], context: dict,
    ) -> None:
        """
        command_introspection: write this command invocation + its reply as
        two real DB nodes — [user: /cmd] -> [assistant: reply] — right on
        the branch, at the moment the command actually happened. /reset is
        excluded — a reset starts a fresh branch and there's nothing for the
        old branch's LLM to be told about.

        This used to stash a log in session state for AgentCycle.run() to
        replay on the *next* turn — indirect, order-fragile (state is a
        single merge-written key, so concurrent commands on sibling
        branches could stomp each other), and it inserted the replay in the
        wrong position (right before whatever the next real message
        happened to be, not at the time the command actually ran). Writing
        the nodes directly here means they land in true chronological
        order and are picked up by context.assemble() exactly like any
        other turn — no separate replay step in agent.py needed.

        Sets context["_command_introspection_tail"] to the new tail node id
        on success. Bridges MUST check this after dispatch() returns and
        advance their own cursor to it (same as they already do for
        /v1/lane/message's returned tail) — otherwise the next real message
        attaches to the pre-command node and these two nodes end up
        orphaned on a dead branch. See gateway/__main__.py's
        handle_lane_command and bridges/discord/commands.py for the two
        current bridges wired up to do this.

        Reply text comes from context["get_output"] — a zero-arg callable
        bridges already populate for their own purposes (the gateway reads
        it to return the HTTP response, Discord reads it to send the
        followup) — so no capture/monkey-patching is needed here.

        Best-effort: silently no-ops if the flag is off, or if this
        bridge's context doesn't carry what we need (runtime + a
        node_id/cursor + get_output).
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
        get_output = context.get("get_output")
        output_str = (get_output() if callable(get_output) else "") or ""
        output_str = output_str.strip() or "(no output)"
        author_id = CommandRegistry._resolve_caller_username(runtime, context)
        try:
            user_node = runtime.db.add_node(
                parent_id=node_id, role="user", content=cmd_str.strip(), author_id=author_id,
            )
            assistant_node = runtime.db.add_node(
                parent_id=user_node.id, role="assistant", content=output_str,
            )
            context["_command_introspection_tail"] = assistant_node.id
        except Exception:
            logger.exception("[commands] command_introspection: failed to record %r", cmd_str)

    @staticmethod
    def _resolve_caller(runtime, context: dict):
        """
        Resolve the TinyCTX.users.models.User of whoever actually ran the
        command. Preference order: an already-resolved User
        (context["caller"]) beats a platform/user_id pair
        (context["caller_platform"] + "caller_user_id"), resolved via
        runtime.users.get_by_platform(). Returns None if the bridge supplied
        neither, or if runtime is unavailable to resolve a platform pair —
        callers should treat that as "unattributable" rather than guessing.
        """
        caller = context.get("caller")
        if caller is not None:
            return caller

        platform = context.get("caller_platform")
        user_id = context.get("caller_user_id")
        if runtime is not None and platform and user_id:
            try:
                from TinyCTX.contracts import Platform
                return runtime.users.get_by_platform(Platform(platform), str(user_id))
            except Exception:
                logger.debug(
                    "[commands] failed to resolve caller for "
                    "platform=%s user_id=%s", platform, user_id, exc_info=True,
                )
                return None
        return None

    @staticmethod
    def _resolve_caller_username(runtime, context: dict) -> str | None:
        """
        Resolve the TinyCTX username of whoever actually ran the command, so
        the replayed [user: ...] turn gets the same 【username】 prefix real
        dialogue gets (see context.py's assemble — a user entry with no
        author_id is missing that prefix entirely and logs an error).
        Thin wrapper over _resolve_caller — see that method for the
        preference order.
        """
        user = CommandRegistry._resolve_caller(runtime, context)
        return getattr(user, "username", None)

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

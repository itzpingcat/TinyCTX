"""
hooks.py — HookRegistry, HookType, Combine, Scratch.

See docs/MODULES-PLAN-P1.md for the design rationale. Summary:

  - One registry for the process: dict[HookType, list[Handler]] that a
    caller looks up and runs inline, on its own stack. Not a bus — no
    queue, no decoupling, no deferred delivery.
  - HookType is a closed enum; each member carries its own Combine
    strategy (and, for STREAM_TEXT, isolate=False) so two callers of one
    stage can never disagree about how return values combine.
  - Scratch is a namespaced per-pass mapping (assembly pass / stream pass)
    that replaces closure-cell state, since handlers are meant to be
    registered once per process lifetime and cannot close over per-turn
    data.

This module is intentionally self-contained: it does not import from
context.py, agent.py, or runtime.py, so it can be adopted incrementally
(Context can delegate to it without every hook stage changing shape at
once — see MODULES-PLAN-P1.md's phased rollout).
"""
from __future__ import annotations

import inspect
import logging
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Combine — how a stage's handler return values combine into one result.
# ---------------------------------------------------------------------------

class Combine(Enum):
    FANOUT = "fanout"      # run all handlers, ignore returns; emit() returns None
    VETO = "veto"          # first falsy (but not None) return wins, short-circuits;
                            # emit() returns False if any handler vetoed, else True
    CHAIN = "chain"        # first positional arg threads through each handler in
                            # turn; a None return leaves the value unchanged; emit()
                            # returns the final value
    COLLECT = "collect"    # gather every non-None return into a list; emit()
                            # returns that list (possibly empty)
    DISPATCH = "dispatch"  # one handler per key, looked up (not iterated); see
                            # HookRegistry.emit_dispatch()


# ---------------------------------------------------------------------------
# HookType — closed enum. Each member is (wire_name, combine, isolate).
# ---------------------------------------------------------------------------

class HookType(Enum):
    """
    A HookType is declared as a 2- or 3-tuple value:
        (wire_name: str, combine: Combine)
        (wire_name: str, combine: Combine, isolate: bool)   # isolate defaults True

    wire_name is the legacy string stage name (e.g. "transform_turn") used by
    Context.register_hook()'s back-compat shim so existing string-literal
    call sites keep working unchanged (see MODULES-PLAN-P1.md's "Stage names
    are unvalidated strings" defect — this is what fixes it: an unknown wire
    name raises instead of silently registering into a dead bucket).

    isolate=False means emit() does NOT wrap each handler call in its own
    try/except — used only for STREAM_TEXT, which runs once per streamed
    token and cannot afford a try/except (and allocation) per handler per
    token. A raising handler on an isolate=False stage propagates.
    """

    STARTUP         = ("startup",         Combine.FANOUT)
    SHUTDOWN        = ("shutdown",        Combine.FANOUT)
    BACKGROUND      = ("background",      Combine.FANOUT)
    INBOUND         = ("inbound",         Combine.CHAIN)
    DELIVER         = ("deliver",         Combine.DISPATCH)

    TURN_START      = ("turn_start",      Combine.FANOUT)
    PRE_ASSEMBLE    = ("pre_assemble",    Combine.FANOUT)
    # PRE_ASSEMBLE_ASYNC is kept as its own member for now, diverging from
    # MODULES-PLAN-P1.md's target end-state ("PRE_ASSEMBLE_ASYNC is gone as a
    # separate type: emit awaiting coroutines makes sync-vs-async a property
    # of the handler, not of the stage"). In THIS codebase the two are not
    # just sync/async of one call site: HOOK_PRE_ASSEMBLE_ASYNC handlers are
    # awaited by AgentCycle.run() via run_async_hooks() BEFORE assemble() is
    # called at all, while HOOK_PRE_ASSEMBLE handlers run synchronously
    # *inside* assemble()'s own step 1, after dialogue is loaded from the DB.
    # Collapsing them into one HookType bucket right now would either
    # double-fire every pre-assemble hook (if both call sites emit the same
    # type) or silently change *when* one of the two groups runs relative to
    # DB-loaded dialogue being available. That is a real behavior change the
    # plan doesn't call out against this specific codebase's split — so it's
    # deliberately deferred rather than folded in during this wiring step.
    PRE_ASSEMBLE_ASYNC = ("pre_assemble_async", Combine.FANOUT)
    FILTER_TURN     = ("filter_turn",     Combine.VETO)
    TRANSFORM_TURN  = ("transform_turn",  Combine.CHAIN)
    POST_ASSEMBLE   = ("post_assemble",   Combine.CHAIN)
    STREAM_START    = ("stream_start",    Combine.FANOUT)
    STREAM_TEXT     = ("stream_text",     Combine.CHAIN, False)
    STREAM_END      = ("stream_end",      Combine.COLLECT)
    POST_COMPLETION = ("post_completion", Combine.COLLECT)
    POST_TURN       = ("post_turn",       Combine.FANOUT)

    def __new__(cls, wire_name: str, combine: Combine, isolate: bool = True):
        obj = object.__new__(cls)
        obj._value_ = wire_name
        obj.wire_name = wire_name
        obj.combine = combine
        obj.isolate = isolate
        return obj

    @classmethod
    def from_wire_name(cls, name: str) -> "HookType":
        for member in cls:
            if member.wire_name == name:
                return member
        raise ValueError(
            f"unknown hook stage {name!r} — not a registered HookType wire name "
            f"(known: {', '.join(m.wire_name for m in cls)}). This used to silently "
            f"register into a dead bucket that nothing drains; it is a hard error now."
        )


# ---------------------------------------------------------------------------
# Scratch — namespaced per-pass mapping, created at the top of a pass and
# dropped at the bottom. Not session state — see module docstring.
# ---------------------------------------------------------------------------

class Scratch:
    """
    Attribute-style mapping for one pass's working data:

        scratch.suppressed_tool = set()
        if entry.index in scratch.suppressed_tool: ...

    Reading an attribute that was never set raises AttributeError (not a
    silent None) so a typo'd key name fails loudly instead of behaving like
    an always-empty default. Use getattr(scratch, "key", default) when a
    handler wants "not set yet" to mean something specific.

    Namespacing is per-caller's own discipline, not enforced structurally:
    two modules using the same attribute name on the same Scratch instance
    will collide. The convention (see MODULES-PLAN-P1.md) is one Scratch
    instance per pass, with each module picking distinct key names — this
    class does not attempt to auto-namespace by module, since attribute
    access needs to stay ergonomic (`scratch.suppressed_tool`, not
    `scratch["ctx_tools"]["suppressed_tool"]`).
    """

    def __repr__(self) -> str:
        return f"Scratch({self.__dict__!r})"


# ---------------------------------------------------------------------------
# HookRegistry
# ---------------------------------------------------------------------------

Handler = Callable[..., Any]


class HookRegistry:
    """
    One registry for the process. register() files a handler under its
    HookType at (priority, registration_seq) order. emit() runs all
    handlers for a type against the type's Combine strategy and returns
    the combined result — the caller does not choose combine behavior,
    the HookType already carries it.

    Not a bus: emit() runs handlers inline, synchronously in the caller's
    stack for a sync handler, awaited for a coroutine handler, and returns
    before the caller's next line.
    """

    def __init__(self) -> None:
        self._handlers: dict[HookType, list[tuple[int, int, Handler]]] = {t: [] for t in HookType}
        self._seq = 0

    def register(self, type: HookType, fn: Handler, *, priority: int = 0) -> None:
        if not isinstance(type, HookType):
            raise TypeError(
                f"HookRegistry.register() requires a HookType member, got {type!r}. "
                f"The enum is closed by design — see MODULES-PLAN-P1.md's "
                f"'Module-defined hook types' deferral."
            )
        self._seq += 1
        self._handlers[type].append((priority, self._seq, fn))
        self._handlers[type].sort(key=lambda e: (e[0], e[1]))

    def unregister(self, type: HookType, fn: Handler) -> None:
        self._handlers[type] = [e for e in self._handlers[type] if e[2] is not fn]

    def handlers_for(self, type: HookType) -> list[Handler]:
        """Ordered handler list for `type`, priority then registration order."""
        return [fn for _, _, fn in self._handlers[type]]

    # ------------------------------------------------------------------
    # emit — fan-out combine strategies (all handlers run/considered)
    # ------------------------------------------------------------------

    async def emit(self, type: HookType, *args, scratch: Scratch | None = None) -> Any:
        """
        Run every handler registered for `type` and combine their return
        values per type.combine. Coroutine handlers are awaited; sync
        handlers are called directly — sync-vs-async is a property of the
        handler, not of the stage (see MODULES-PLAN-P1.md: PRE_ASSEMBLE_ASYNC
        is gone as a separate type for exactly this reason).

        `scratch`, if given, is appended as a final keyword-ish positional:
        handlers that want it declare a trailing `scratch` parameter; a
        handler that doesn't declare one is called without it (inspected
        once per call via signature — acceptable at this call frequency;
        STREAM_TEXT's fast path in emit_stream_text() below skips this
        entirely, which is the isolate=False path this cost is not paid on).
        """
        combine = type.combine
        handlers = self.handlers_for(type)

        if combine == Combine.DISPATCH:
            raise TypeError(
                f"{type} uses Combine.DISPATCH — call emit_dispatch(), not emit()."
            )

        if combine == Combine.FANOUT:
            for fn in handlers:
                await self._call(type, fn, args, scratch)
            return None

        if combine == Combine.VETO:
            for fn in handlers:
                result = await self._call(type, fn, args, scratch)
                if result is False:
                    return False
            return True

        if combine == Combine.CHAIN:
            # First positional arg is the value threaded through the chain.
            if not args:
                raise TypeError(f"{type} is Combine.CHAIN and requires at least one positional arg to thread")
            value = args[0]
            rest = args[1:]
            for fn in handlers:
                result = await self._call(type, fn, (value, *rest), scratch)
                if result is not None:
                    value = result
            return value

        if combine == Combine.COLLECT:
            collected = []
            for fn in handlers:
                result = await self._call(type, fn, args, scratch)
                if result is not None:
                    collected.append(result)
            return collected

        raise AssertionError(f"unhandled combine strategy {combine!r}")  # pragma: no cover

    async def emit_dispatch(self, type: HookType, key: str, *args, scratch: Scratch | None = None) -> Any:
        """
        DISPATCH combine: exactly one handler is looked up by `key` (not
        iterated) and called, or None if nothing is registered for that
        key. Handlers for a DISPATCH type must be registered with a
        `key=` kwarg via register_dispatch() (below) rather than plain
        register(), since ordinary (priority, fn) registration has no
        place to carry the key.
        """
        if type.combine != Combine.DISPATCH:
            raise TypeError(f"{type} is not Combine.DISPATCH")
        table: dict[str, Handler] = getattr(self, "_dispatch_tables", {}).get(type, {})
        fn = table.get(key)
        if fn is None:
            logger.warning("[hooks] emit_dispatch: no handler registered for %s key=%r", type, key)
            return None
        return await self._call(type, fn, args, scratch)

    def register_dispatch(self, type: HookType, key: str, fn: Handler) -> None:
        if type.combine != Combine.DISPATCH:
            raise TypeError(f"{type} is not Combine.DISPATCH")
        if not hasattr(self, "_dispatch_tables"):
            self._dispatch_tables: dict[HookType, dict[str, Handler]] = {}
        self._dispatch_tables.setdefault(type, {})[key] = fn

    # ------------------------------------------------------------------
    # Fast path for STREAM_TEXT (isolate=False, no try/except, no scratch
    # signature inspection) — this runs once per streamed token.
    # ------------------------------------------------------------------

    def emit_stream_text_sync(self, text: str) -> str:
        """
        Synchronous, no-wrapper-allocation path for STREAM_TEXT. Handlers on
        this stage are the object-protocol style (reset/process/flush) kept
        for the hot per-token path — see agent.py's stream_text_hooks. This
        method exists so a raising handler here propagates immediately
        (isolate=False is a deliberate, documented behavior, not an
        oversight) rather than going through the generic emit()'s per-handler
        try/except.
        """
        for fn in self.handlers_for(HookType.STREAM_TEXT):
            text = fn(text)
        return text

    # ------------------------------------------------------------------
    # Internal call helper — handles async/sync dispatch, scratch injection,
    # and per-type isolation (try/except-and-log, except isolate=False).
    # ------------------------------------------------------------------

    async def _call(self, type: HookType, fn: Handler, args: tuple, scratch: Scratch | None) -> Any:
        call_args = args
        if scratch is not None and self._wants_scratch(fn):
            call_args = (*args, scratch)

        if type.isolate:
            try:
                result = fn(*call_args)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception:
                logger.exception("Hook %r raised on stage %s", getattr(fn, "__name__", fn), type)
                return None
        else:
            result = fn(*call_args)
            if inspect.isawaitable(result):
                result = await result  # pragma: no cover - STREAM_TEXT handlers are sync today
            return result

    @staticmethod
    def _wants_scratch(fn: Handler) -> bool:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False
        return "scratch" in params

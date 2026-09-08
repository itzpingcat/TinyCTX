"""
decorators.py — @tool, @hook, @command, @prompt.

Every framework attachment point is an explicit decorator; nothing is wired
by naming convention or method-name matching. A decorator runs at class
DEFINITION time, before any instance exists — it can only record metadata
onto the function object (in a `_tinyctx_binding` attribute); actual
registration happens later, when a loader walks an instantiated class's
tagged methods (module_registry.py's class-path loader, not added in this
file) and calls the real registration API (cycle.context.register_hook,
cycle.tool_handler.register_tool, etc.) using that recorded metadata plus
the now-bound method.

Hook/tool/prompt/command bodies still take raw framework objects exactly as
they do today. These decorators change *how a handler gets attached*, not
what it's handed once it runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from TinyCTX.hooks import HookType

_UNSET = object()  # distinguishes "permissions=None was explicitly passed" from "omitted"


# ---------------------------------------------------------------------------
# Binding records — one per decorated method, stashed on the function object.
# A method may carry more than one binding only if a caller deliberately
# stacks decorators (not a supported pattern today); walk_bindings() below
# yields all of them regardless.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolBinding:
    permissions: Any                    # set[Permission] | None | Callable — required
    listing_permissions: Any = None
    always_on: bool = False
    name: str | None = None
    timeout: float = 600.0              # deliberately generous; a tool doing real I/O shouldn't be timed out by a default meant to catch runaway loops


@dataclass(frozen=True)
class HookBinding:
    type: HookType
    priority: int = 0


@dataclass(frozen=True)
class CommandBinding:
    namespace: str
    sub: str = ""
    permissions: Any = _UNSET           # required keyword; _UNSET sentinel lets us
                                         # distinguish "omitted" from "explicitly None"
    help: str = ""
    params: list[tuple[str, type, str]] | None = None


@dataclass(frozen=True)
class PromptBinding:
    role: str = "system"
    priority: int = 0
    name: str | None = None


_BINDING_ATTR = "_tinyctx_bindings"


def _attach(fn: Callable, binding) -> Callable:
    existing = getattr(fn, _BINDING_ATTR, None)
    bindings = list(existing) if existing else []
    bindings.append(binding)
    setattr(fn, _BINDING_ATTR, bindings)
    return fn


def bindings_of(fn: Callable) -> list:
    """All binding records attached to `fn` (empty list if none)."""
    return list(getattr(fn, _BINDING_ATTR, ()))


def walk_bindings(instance) -> list[tuple[str, Any, Callable]]:
    """
    Walk `instance`'s methods for decorator-tagged bindings.
    Returns a list of (method_name, binding, bound_method) triples, in
    definition order (Python preserves class-dict insertion order, which is
    source order) — a loader can rely on this for stable, deterministic
    registration.
    """
    results: list[tuple[str, Any, Callable]] = []
    seen_names: set[str] = set()
    for cls in type(instance).__mro__:
        for name, attr in vars(cls).items():
            if name in seen_names or not callable(attr):
                continue
            bindings = bindings_of(attr)
            if not bindings:
                continue
            seen_names.add(name)
            bound = getattr(instance, name)
            for binding in bindings:
                results.append((name, binding, bound))
    return results


# ---------------------------------------------------------------------------
# @tool
# ---------------------------------------------------------------------------

def tool(*, permissions, listing_permissions=None, always_on: bool = False,
         name: str | None = None, timeout: float = 600.0):
    """
    `permissions` is required — omitting it is a TypeError at class-
    definition time (Python's own required-keyword-argument enforcement on
    this decorator factory, which is called while the class body executes).
    Accepts all three existing forms unchanged: a set[Permission] (static),
    None (explicitly ungated), or a callable (evaluated per call against
    the tool's own arguments — load-bearing for modules/shell and
    modules/present, both of which classify permissions from the actual
    call arguments rather than a fixed set).
    """
    binding = ToolBinding(
        permissions=permissions,
        listing_permissions=listing_permissions,
        always_on=always_on,
        name=name,
        timeout=timeout,
    )

    def decorator(fn: Callable) -> Callable:
        return _attach(fn, binding)

    return decorator


# ---------------------------------------------------------------------------
# @hook
# ---------------------------------------------------------------------------

def hook(type: HookType, *, priority: int = 0):
    """
    `type.` autocompletes to the full HookType set in an editor; a typo is
    an AttributeError on a known enum rather than a silent miss into a
    dead string-keyed bucket.
    """
    if not isinstance(type, HookType):
        raise TypeError(f"@hook requires a HookType member, got {type!r}")
    binding = HookBinding(type=type, priority=priority)

    def decorator(fn: Callable) -> Callable:
        return _attach(fn, binding)

    return decorator


# ---------------------------------------------------------------------------
# @command
# ---------------------------------------------------------------------------

def command(namespace: str, sub: str = "", *, permissions=_UNSET,
            help: str = "", params: list[tuple[str, type, str]] | None = None):
    """
    Wraps CommandRegistry's existing semantics — `permissions` is required
    (the _UNSET-vs-None distinction, same as @tool), asserted at startup.
    The handler contract (return the string to send instead of calling
    send(); (args, context) signature, not typed kwargs) is unchanged by
    this decorator; it just moves the registration call from a loose
    commands.register(...) to the line above the method.
    """
    if permissions is _UNSET:
        raise TypeError("@command requires permissions= (pass None for explicitly ungated)")
    binding = CommandBinding(namespace=namespace, sub=sub, permissions=permissions,
                              help=help, params=params)

    def decorator(fn: Callable) -> Callable:
        return _attach(fn, binding)

    return decorator


# ---------------------------------------------------------------------------
# @prompt
# ---------------------------------------------------------------------------

def prompt(*, role: str = "system", priority: int = 0, name: str | None = None):
    """
    Wraps Context.register_prompt(pid, provider, *, role, priority). `pid`
    defaults to `<module_name>.<method_name>` at registration time (the
    loader knows the module name; this decorator only records the optional
    override). The provider body still takes `ctx` (the raw assembly
    Context) — unchanged by this decorator.
    """
    binding = PromptBinding(role=role, priority=priority, name=name)

    def decorator(fn: Callable) -> Callable:
        return _attach(fn, binding)

    return decorator

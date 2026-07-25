"""
modules/memory/scopes.py

Scope grammar + per-cycle visible-scope resolution for the memory graph.

Scope is an information-isolation mechanism, NOT an ownership tag. A node's
`scope` restricts *where it is visible*, not who it is about. Most Person nodes
should be `global`. Narrow scopes (`user:<name>`, `guild:<name>`) are only for
sensitive / personal / bucket-local information.

Grammar
-------
    global                      -- the shared bucket, visible everywhere
    <name>:<target>             -- e.g. user:itzpingcat, guild:my-server

`<name>` is a lowercase scope kind; `<target>` is an arbitrary non-empty token
with whitespace collapsed to underscores. The same grammar is reused by the
`pinned` field (a pin is a scope-shaped "always surface here" statement).

Both `scope` and `pinned` are validated by `is_valid_scope()` at the tool layer.
"""
from __future__ import annotations

import re

GLOBAL = "global"

# A scope kind is a short lowercase identifier; a target is any run of
# non-whitespace, non-colon characters (colons separate kind from target).
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]*:[^\s:][^\s]*$")

# Kinds we normalise conversation environment into. `user:` for participants,
# `guild:` for the server/space a conversation happens in.
KIND_USER = "user"
KIND_GUILD = "guild"


def normalize_target(target: str) -> str:
    """Collapse whitespace to underscores and lowercase a scope target token."""
    return re.sub(r"\s+", "_", target.strip()).lower()


def make_scope(kind: str, target: str) -> str:
    """Build a `kind:target` scope string from parts."""
    return f"{kind}:{normalize_target(target)}"


def is_valid_scope(scope: str) -> bool:
    """
    True if `scope` is the literal `global` or matches `kind:target` grammar.
    Empty string is NOT a valid scope (but IS a valid *unpinned* pin value —
    callers check `== ""` separately for pins).
    """
    if scope == GLOBAL:
        return True
    return bool(_SCOPE_RE.match(scope))


def parse_scope(scope: str) -> tuple[str, str | None]:
    """
    Return (kind, target). For `global` the kind is 'global' and target None.
    Raises ValueError on an invalid scope so callers fail loudly.
    """
    if scope == GLOBAL:
        return GLOBAL, None
    if not is_valid_scope(scope):
        raise ValueError(f"invalid scope: {scope!r}")
    kind, target = scope.split(":", 1)
    return kind, target


def resolve_scopes(env: dict, active_users: set[str]) -> set[str]:
    """
    Compute the set of scopes visible to the current AgentCycle.

    Always includes `global`. Adds `guild:<server>` when the conversation is in
    a named server/space. Adds `user:<name>` for every recent human participant.

    Args:
        env: environment snapshot with optional keys `server_name`,
             `channel_name`, `platform` (as stored in the cycle state delta).
        active_users: TinyCTX usernames of humans who spoke in the last N turns.

    Returns:
        A set of scope strings. This set is the single authority for read
        visibility — every read path filters `WHERE e.scope IN <this set>`.
    """
    visible: set[str] = {GLOBAL}

    server = (env or {}).get("server_name")
    if server:
        visible.add(make_scope(KIND_GUILD, str(server)))

    for user in active_users:
        if user:
            visible.add(make_scope(KIND_USER, str(user)))

    return visible


def writable_scopes(visible: set[str]) -> set[str]:
    """
    The scopes a librarian running in this cycle may WRITE to. Identical to the
    visible set: an extractor cannot write `user:carl` unless Carl is present.
    Kept as a distinct function so the write-vs-read intent is explicit at call
    sites and can diverge later without touching callers.
    """
    return set(visible)

"""
module.py — Module base class.

Deliberately minimal at this stage: no AppContext/TurnContext/CommandContext
facade — hook/tool/prompt/command bodies still take raw framework objects
(runtime, cycle, context, a plain dict), the same objects the pre-Module
register_runtime()/register_agent() function pair used to hand them (that
convention is gone as of MODULES-PLAN-P1.md's P3 — every module is a Module
subclass now). This file gives a module author one class to subclass and
four decorators (in decorators.py) to tag methods with, replacing that old
"two loose functions plus a boilerplate config merge, copy-pasted into ten
modules" shape.
"""
from __future__ import annotations

import re
from typing import Any


def _default_name(cls: type) -> str:
    """CamelCase class name -> snake_case module name, e.g. CtxTools -> ctx_tools."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", cls.__name__)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


class ToolError(Exception):
    """
    Raised by a @tool body to signal an EXPECTED failure the model should
    read and adapt to — not a crash. There is no result envelope; a tool
    just returns a string. ToolError is the one exception type the
    framework catches and renders consistently, so error formatting is the
    framework's job rather than each author's ad-hoc "Error: ..." string
    prefix.
    """


class Module:
    """
    Base class for a decorator-based module. A module directory (or single
    .py file) exports one Module subclass; the loader instantiates it once
    and walks its decorator-tagged methods (see decorators.py) to register
    them.

    Class attributes (all optional, all with safe defaults):

        name:         str                      — defaults to snake_case class name
        settings:     dict                     — declarative settings schema (see below)
        dependencies: tuple[str, ...]           — optional-import names; loader skips
                                                   this module with a log line (not a
                                                   crash) if any fails to import
        requires:     tuple[str, ...]           — other module names, for load order
        platforms:    frozenset[str] | None      — None = every platform; else gates
                                                   TURN_START/tool registration to
                                                   matching turns
        unsafe:       bool                      — flagged in settings UIs

    `settings` is a declarative schema, not a flat defaults dict:

        settings = {
            "max_note_chars": {
                "default": 8000,
                "type": "int",
                "description": "Longest note the agent may write.",
            },
        }

    resolve_settings() merges it once against config.extra[<module name>] and
    returns a plain dict of resolved values — this is what a loader assigns
    to self.config on the instance, replacing the ten-site copy-pasted
    "hasattr config.extra, .get(name, {}), merge with defaults" boilerplate.
    """

    settings: dict[str, dict[str, Any]] = {}
    dependencies: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    platforms: frozenset[str] | None = None
    unsafe: bool = False

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if "name" not in cls.__dict__:
            cls.name = _default_name(cls)

    def resolve_settings(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        """
        Merge this module's settings schema's defaults with
        config.extra[self.name], if present. `extra` is the FULL
        config.extra mapping (all modules' overrides); this method reads
        only its own namespace out of it — matching today's
        `config.extra.get(<name>, {})` convention exactly, just without the
        eight-line hasattr/isinstance dance repeated at every call site.
        """
        resolved = {key: spec.get("default") for key, spec in self.settings.items()}
        if extra and isinstance(extra, dict):
            overrides = extra.get(self.name, {})
            if isinstance(overrides, dict):
                for key in resolved:
                    if key in overrides:
                        resolved[key] = overrides[key]
        return resolved

    def dependencies_satisfied(self) -> tuple[bool, str | None]:
        """
        Returns (True, None) if every name in `dependencies` imports
        cleanly, else (False, missing_name). The loader uses this to skip a
        module with a clear log line instead of failing at import with a
        stack trace: TinyCTX never installs anything at runtime, so a
        module whose optional dependency is absent must degrade to
        "not loaded", not crash the process.
        """
        import importlib
        for dep_name in self.dependencies:
            try:
                importlib.import_module(dep_name)
            except ImportError:
                return False, dep_name
        return True, None

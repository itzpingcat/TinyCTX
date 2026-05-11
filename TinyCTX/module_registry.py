"""
module_registry.py — Module loading and per-cycle wiring.

ModuleRegistry replaces the old runtime._load_modules() + _ContextProxy
pattern. Modules now expose two optional functions:

  def register_runtime(runtime: Runtime) -> None:
      # Called once at startup.
      # Create singletons (store, indexer, embedder) as locals.
      # Register tools on runtime's ToolCallHandler template.
      # Register commands.
      # Register background (post-turn) hooks via runtime.register_background_hook().
      # Define register_agent as a closure over the singletons (see below).

  def register_agent(cycle: AgentCycle) -> None:
      # Called per AgentCycle.__init__.
      # Register prompt providers on cycle.context.
      # Register pre-assemble hooks on cycle.context.
      # Typically defined as a closure inside register_runtime so it can
      # capture singletons (store, indexer, embedder) without module_env.

Backward compatibility
----------------------
Modules that still expose the old-style register(agent) function are
wrapped automatically: register(runtime) is called at startup, and
register(cycle) is called per cycle. This lets old modules continue to
work without changes while new modules use the cleaner two-function API.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from TinyCTX.agent import AgentCycle

logger = logging.getLogger(__name__)

MODULES_DIR = Path(__file__).parent / "modules"


class ModuleRegistry:
    """
    Loads modules at startup and wires them into each new AgentCycle.

    Usage:
        registry = ModuleRegistry()
        registry.load_modules(runtime)   # called once in Runtime.start()
        ...
        registry.register_agent(cycle)   # called in AgentCycle.__init__
    """

    def __init__(self) -> None:
        # List of register_agent callables collected during load_modules().
        # Each entry is called with the AgentCycle every time a new cycle starts.
        self._agent_registrations: list[callable] = []

    def load_modules(self, runtime) -> None:
        """
        Scan modules/ and call register_runtime (or legacy register) on each.
        Collects register_agent functions for later per-cycle wiring.
        """
        if not MODULES_DIR.exists():
            return
        for entry in sorted(MODULES_DIR.iterdir()):
            if not entry.is_dir():
                continue
            if not ((entry / "__main__.py").exists() or (entry / "__init__.py").exists()):
                continue
            module_name = f"TinyCTX.modules.{entry.name}"
            try:
                mod = self._find_module(module_name, entry.name)
                if mod is None:
                    continue
                self._register_one(mod, runtime, entry.name)
            except Exception:
                logger.exception("Failed to load module '%s'", entry.name)

    def _find_module(self, module_name: str, entry_name: str):
        """Import the module object that has register_runtime, register_agent, or register."""
        for suffix in (".__main__", ""):
            try:
                candidate = importlib.import_module(module_name + suffix)
                if (
                    hasattr(candidate, "register_runtime")
                    or hasattr(candidate, "register_agent")
                    or hasattr(candidate, "register")
                ):
                    return candidate
            except ModuleNotFoundError:
                continue
        logger.warning("Module '%s' has no register_runtime/register_agent/register — skipping", entry_name)
        return None

    def _register_one(self, mod, runtime, entry_name: str) -> None:
        """Call register_runtime (or legacy register) and collect register_agent."""

        if hasattr(mod, "register_runtime"):
            # New-style: register_runtime at startup, register_agent per cycle.
            mod.register_runtime(runtime)
            logger.info("Loaded module '%s' (register_runtime)", entry_name)
            if hasattr(mod, "register_agent"):
                self._agent_registrations.append(mod.register_agent)

        elif hasattr(mod, "register"):
            # Legacy: single register(agent) function.
            # Call it now with the runtime for startup wiring.
            # Also register a per-cycle shim that calls register(cycle) so
            # old modules that register prompts/hooks also work per-cycle.
            mod.register(runtime)
            logger.info("Loaded module '%s' (legacy register)", entry_name)
            # Register a per-cycle shim: call register(cycle) for modules
            # that register prompts / context hooks (not singletons).
            # We detect per-cycle intent by checking if the module registers
            # any prompt or hook via the _ContextProxy during register(runtime)
            # — but since we no longer have _ContextProxy, we rely on modules
            # to expose register_agent. Legacy modules that only register tools
            # (no context hooks) don't need per-cycle calls.
            # For full backward compat, we call register(cycle) per cycle too.
            # This is safe: tool_handler.register_tool() is idempotent (overwrites same key).
            self._agent_registrations.append(
                _make_legacy_per_cycle_shim(mod.register, entry_name)
            )

        elif hasattr(mod, "register_agent"):
            # Module has only register_agent (no startup work).
            self._agent_registrations.append(mod.register_agent)
            logger.info("Loaded module '%s' (register_agent only)", entry_name)

    def register_agent(self, cycle: "AgentCycle") -> None:
        """
        Wire all modules into a newly constructed AgentCycle.
        Called from AgentCycle.__init__.
        """
        for fn in self._agent_registrations:
            try:
                fn(cycle)
            except Exception:
                logger.exception("register_agent raised for cycle (fn=%s)", getattr(fn, "__name__", fn))


def _make_legacy_per_cycle_shim(register_fn, entry_name: str):
    """Return a per-cycle function that calls the legacy register(agent) with a cycle."""
    def _shim(cycle):
        try:
            register_fn(cycle)
        except Exception:
            logger.exception("Legacy per-cycle register raised for module '%s'", entry_name)
    _shim.__name__ = f"_legacy_shim_{entry_name}"
    return _shim

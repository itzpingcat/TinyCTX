"""
module_registry.py — Module loading and per-cycle wiring.

Modules expose two functions:

  def register_runtime(runtime: Runtime) -> None:
      # Called once at startup.
      # Build singletons, register commands, background hooks, etc.

  def register_agent(cycle: AgentCycle) -> None:
      # Called per AgentCycle after tool_handler and context are live.
      # Register tools, prompt providers, pre-assemble hooks.

Both are optional. A module with only register_agent does no startup work.
A module with only register_runtime does no per-cycle wiring.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from TinyCTX.agent import AgentCycle

logger = logging.getLogger(__name__)

MODULES_DIR = Path(__file__).parent / "modules"
CUSTOM_MODULES_DIR = Path(__file__).parent / "custom_modules"


class ModuleRegistry:
    """
    Loads modules at startup and wires them into each new AgentCycle.

    Usage:
        registry = ModuleRegistry()
        registry.load_modules(runtime)   # called once in Runtime.start()
        registry.register_agent(cycle)   # called in AgentCycle.run()
    """

    def __init__(self) -> None:
        self._agent_registrations: list[Callable] = []

    def load_modules(self, runtime) -> None:
        """Scan modules/ and custom_modules/ and call register_runtime on each."""
        self._load_from_dir(MODULES_DIR, runtime, import_prefix="TinyCTX.modules")
        self._load_from_dir(CUSTOM_MODULES_DIR, runtime, import_prefix=None)

        print(f"[module_registry] done — {len(self._agent_registrations)} register_agent hook(s) queued")
        logger.info(
            "[module_registry] done — %d register_agent hook(s) queued",
            len(self._agent_registrations),
        )

    def _load_from_dir(self, modules_dir: Path, runtime, import_prefix: str | None) -> None:
        """Scan one modules directory and register all valid modules found."""
        if not modules_dir.exists():
            logger.debug("[module_registry] skipping missing dir: %s", modules_dir)
            return

        entries = sorted(e for e in modules_dir.iterdir() if e.is_dir())
        print(f"[module_registry] scanning {modules_dir.name}/ — {len(entries)} candidate(s)")
        logger.info("[module_registry] scanning %s — %d candidate(s)", modules_dir, len(entries))

        for entry in entries:
            has_main = (entry / "__main__.py").exists()
            has_init = (entry / "__init__.py").exists()
            if not (has_main or has_init):
                logger.debug("[module_registry] skipping '%s' (no __main__.py or __init__.py)", entry.name)
                continue

            print(f"[module_registry] loading '{entry.name}' from {modules_dir.name}/")
            logger.info("[module_registry] loading '%s' from %s", entry.name, modules_dir.name)
            try:
                if import_prefix is not None:
                    mod = self._find_module(f"{import_prefix}.{entry.name}", entry.name)
                else:
                    mod = self._find_module_from_path(entry)
                if mod is None:
                    continue
                self._register_one(mod, runtime, entry.name)
            except Exception:
                print(f"[module_registry] ERROR: failed to load module '{entry.name}'")
                logger.exception("[module_registry] failed to load module '%s'", entry.name)

    def _find_module_from_path(self, entry: Path):
        """Load a module from a filesystem path without requiring it to be a package."""
        for filename in ("__main__.py", "__init__.py"):
            fpath = entry / filename
            if not fpath.exists():
                continue
            fqn = f"custom_modules.{entry.name}.{filename[:-3]}"
            try:
                spec = importlib.util.spec_from_file_location(fqn, fpath)
                candidate = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(candidate)
                has_rt = hasattr(candidate, "register_runtime")
                has_ra = hasattr(candidate, "register_agent")
                if has_rt or has_ra:
                    logger.debug(
                        "[module_registry] '%s' loaded from path (register_runtime=%s, register_agent=%s)",
                        entry.name, has_rt, has_ra,
                    )
                    return candidate
            except Exception:
                logger.exception("[module_registry] error loading '%s' from path", entry.name)
                return None
        logger.warning("[module_registry] '%s' has no register_runtime/register_agent — skipping", entry.name)
        return None

    def _find_module(self, module_name: str, entry_name: str):
        """Import __main__ then package; return first with register_runtime or register_agent."""
        for suffix in (".__main__", ""):
            fqn = module_name + suffix
            try:
                candidate = importlib.import_module(fqn)
                has_rt = hasattr(candidate, "register_runtime")
                has_ra = hasattr(candidate, "register_agent")
                if has_rt or has_ra:
                    print(f"[module_registry] '{entry_name}' found in {fqn} (register_runtime={has_rt}, register_agent={has_ra})")
                    logger.debug(
                        "[module_registry] '%s' found in %s (register_runtime=%s, register_agent=%s)",
                        entry_name, fqn, has_rt, has_ra,
                    )
                    return candidate
                else:
                    logger.debug(
                        "[module_registry] '%s' imported from %s but has no register_* — trying next",
                        entry_name, fqn,
                    )
            except ModuleNotFoundError as e:
                print(f"[module_registry] '{entry_name}' not importable as {fqn}: {e}")
                logger.debug("[module_registry] '%s' not importable as %s: %s", entry_name, fqn, e)
                continue
            except Exception:
                print(f"[module_registry] ERROR importing '{entry_name}' as {fqn}")
                logger.exception("[module_registry] error importing '%s' as %s", entry_name, fqn)
                return None

        print(f"[module_registry] '{entry_name}' has no register_runtime/register_agent — skipping")
        logger.warning("[module_registry] '%s' has no register_runtime/register_agent — skipping", entry_name)
        return None

    def _register_one(self, mod, runtime, entry_name: str) -> None:
        if hasattr(mod, "register_runtime"):
            print(f"[module_registry] calling register_runtime for '{entry_name}'")
            logger.info("[module_registry] calling register_runtime for '%s'", entry_name)
            mod.register_runtime(runtime)
            print(f"[module_registry] register_runtime done for '{entry_name}'")
            logger.info("[module_registry] register_runtime done for '%s'", entry_name)
            if hasattr(mod, "register_agent"):
                self._agent_registrations.append(mod.register_agent)
                logger.debug("[module_registry] queued register_agent for '%s'", entry_name)
        elif hasattr(mod, "register_agent"):
            self._agent_registrations.append(mod.register_agent)
            print(f"[module_registry] queued register_agent (no runtime) for '{entry_name}'")
            logger.info("[module_registry] queued register_agent (no runtime) for '%s'", entry_name)

    def register_agent(self, cycle: "AgentCycle") -> None:
        """Wire all modules into a newly constructed AgentCycle."""
        logger.debug("[module_registry] register_agent called, %d hook(s)", len(self._agent_registrations))
        for fn in self._agent_registrations:
            try:
                logger.debug("[module_registry] calling %s", getattr(fn, "__name__", fn))
                fn(cycle)
            except Exception:
                logger.exception("[module_registry] register_agent raised (fn=%s)", getattr(fn, "__name__", fn))

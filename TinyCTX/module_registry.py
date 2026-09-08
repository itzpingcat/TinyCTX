"""
module_registry.py — Module loading and per-cycle wiring.

A module directory (or single .py file) exports one Module subclass
(TinyCTX/module.py) with @tool/@hook/@command/@prompt-decorated methods
(TinyCTX/decorators.py). The loader instantiates it once, at load_modules()
time, and walks its tagged methods to register them — see
_register_module_class (runtime-scoped: STARTUP, @command) and
_wire_module_instance (per-cycle-scoped: @tool, most @hook types, @prompt),
called from register_agent() for every new AgentCycle.

MODULES-PLAN-P1.md's earlier phases supported a legacy function-pair shape
(register_runtime(runtime)/register_agent(cycle)) alongside this one, for
migrating modules one at a time without a flag-day cutover. That shape is
gone now that every module in this repo is Module-class-based (P3) — a
module lacking a Module subclass is skipped with a warning, not silently
treated as function-based.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from TinyCTX.decorators import CommandBinding, HookBinding, PromptBinding, ToolBinding, walk_bindings
from TinyCTX.hooks import HookType
from TinyCTX.module import Module

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
        # Module-class instances found at load_modules() time, wired into
        # each AgentCycle in register_agent().
        self._module_instances: list[Module] = []

    def load_modules(self, runtime) -> None:
        """Scan modules/ and custom_modules/ and instantiate each Module class found."""
        self._load_from_dir(MODULES_DIR, runtime, import_prefix="TinyCTX.modules")
        self._load_from_dir(CUSTOM_MODULES_DIR, runtime, import_prefix=None)

        print(f"[module_registry] done — {len(self._module_instances)} module(s) loaded")
        logger.info("[module_registry] done — %d module(s) loaded", len(self._module_instances))

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
                if self._find_module_class(candidate) is not None:
                    logger.debug("[module_registry] '%s' loaded from path", entry.name)
                    return candidate
            except Exception:
                logger.exception("[module_registry] error loading '%s' from path", entry.name)
                return None
        logger.warning("[module_registry] '%s' has no Module class — skipping", entry.name)
        return None

    def _find_module(self, module_name: str, entry_name: str):
        """Import __main__ then package; return whichever defines a Module class."""
        for suffix in (".__main__", ""):
            fqn = module_name + suffix
            try:
                candidate = importlib.import_module(fqn)
                if self._find_module_class(candidate) is not None:
                    print(f"[module_registry] '{entry_name}' found in {fqn}")
                    logger.debug("[module_registry] '%s' found in %s", entry_name, fqn)
                    return candidate
                else:
                    logger.debug(
                        "[module_registry] '%s' imported from %s but has no Module class — trying next",
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

        print(f"[module_registry] '{entry_name}' has no Module class — skipping")
        logger.warning("[module_registry] '%s' has no Module class — skipping", entry_name)
        return None

    def _register_one(self, mod, runtime, entry_name: str) -> None:
        module_class = self._find_module_class(mod)
        if module_class is not None:
            self._register_module_class(module_class, runtime, entry_name)

    @staticmethod
    def _find_module_class(mod) -> type[Module] | None:
        """First Module subclass defined in `mod`, or None. `attr.__module__
        == mod.__name__` excludes Module itself and any Module subclass the
        file merely imported rather than defined."""
        for attr in vars(mod).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, Module)
                and attr is not Module
                and attr.__module__ == mod.__name__
            ):
                return attr
        return None

    def _register_module_class(self, module_class: type[Module], runtime, entry_name: str) -> None:
        instance = module_class()
        ok, missing = instance.dependencies_satisfied()
        if not ok:
            print(f"[module_registry] '{entry_name}' skipped — missing dependency '{missing}'")
            logger.warning("[module_registry] '%s' skipped — missing dependency '%s'", entry_name, missing)
            return
        instance.config = instance.resolve_settings(getattr(getattr(runtime, "config", None), "extra", None))

        # CommandRegistry lives on the runtime, not the per-turn AgentCycle,
        # so CommandBinding registers here rather than in _wire_module_instance.
        # STARTUP is the other runtime-scoped type (replaces register_runtime()):
        # called once, now, with `runtime`.
        for name, binding, bound in walk_bindings(instance):
            if isinstance(binding, CommandBinding) and hasattr(runtime, "commands"):
                runtime.commands.register(
                    binding.namespace, binding.sub, bound,
                    help=binding.help, params=binding.params,
                    required_permissions=binding.permissions,
                )
            elif isinstance(binding, HookBinding) and binding.type is HookType.STARTUP:
                try:
                    bound(runtime)
                except Exception:
                    logger.exception(
                        "[module_registry] STARTUP hook '%s' raised for module '%s'",
                        name, instance.name,
                    )

        self._module_instances.append(instance)
        print(f"[module_registry] '{entry_name}' loaded as Module class '{module_class.__name__}'")
        logger.info("[module_registry] '%s' loaded as Module class '%s'", entry_name, module_class.__name__)

    # HookType members Context.assemble() actually reads out of its own
    # _hooks dict; every other type needs its own home (see below) rather
    # than landing in that dict, where nothing would ever call it.
    _CONTEXT_HOOK_TYPES = frozenset({
        HookType.PRE_ASSEMBLE, HookType.PRE_ASSEMBLE_ASYNC,
        HookType.FILTER_TURN, HookType.TRANSFORM_TURN,
        HookType.POST_ASSEMBLE, HookType.POST_COMPLETION,
    })

    def _wire_module_instance(self, instance: Module, cycle: "AgentCycle") -> None:
        for name, binding, bound in walk_bindings(instance):
            try:
                if isinstance(binding, ToolBinding):
                    cycle.tool_handler.register_tool(
                        bound,
                        name=binding.name,
                        always_on=binding.always_on,
                        required_permissions=binding.permissions,
                        listing_permissions=binding.listing_permissions,
                    )
                elif isinstance(binding, HookBinding):
                    if binding.type in self._CONTEXT_HOOK_TYPES:
                        cycle.context.register_hook(binding.type.wire_name, bound, priority=binding.priority)
                    elif binding.type is HookType.POST_TURN:
                        cycle.post_turn_hooks.append(bound)
                    elif binding.type is HookType.TURN_START:
                        # No emitter exists for this stage yet (MODULES-PLAN-P1.md
                        # notes it "replaces per-turn wiring"): run it now, once,
                        # at this same per-cycle wiring point.
                        bound(cycle)
                    else:
                        logger.warning(
                            "[module_registry] '%s' declares @hook(%s) from module '%s', "
                            "which has no per-cycle wiring yet (STREAM_TEXT/START/END need "
                            "stream-pass Scratch, not built; STARTUP/SHUTDOWN/BACKGROUND/DELIVER "
                            "are runtime-scoped, not cycle-scoped) — not registered anywhere.",
                            name, binding.type, instance.name,
                        )
                elif isinstance(binding, PromptBinding):
                    pid = binding.name or f"{instance.name}.{name}"
                    cycle.context.register_prompt(pid, bound, role=binding.role, priority=binding.priority)
                elif isinstance(binding, CommandBinding):
                    pass  # registered in _register_module_class, once per runtime
            except Exception:
                logger.exception(
                    "[module_registry] failed wiring binding '%s' (%s) from module '%s'",
                    name, type(binding).__name__, instance.name,
                )

    def register_agent(self, cycle: "AgentCycle") -> None:
        """Wire all modules into a newly constructed AgentCycle."""
        for instance in self._module_instances:
            self._wire_module_instance(instance, cycle)

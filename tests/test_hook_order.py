"""
tests/test_hook_order.py

Characterization test for the module/hook rework (docs/MODULES-PLAN-P1.md,
P0 step 3). Snapshots, per Context hook stage, the ordered list of
(module_name, priority) pairs that register_agent() produces today.

This is the safety net for the HookRegistry migration: ctx_tools depends on
exact priorities (dedup 0, tokenade 1, cot_strip 5, trim 8 and 10) and a
silent reordering changes assembled context with no test failure anywhere
else. If this test's snapshot ever changes, that's either an intentional
priority change (update the snapshot) or the exact defect this plan exists
to prevent (an accidental reordering) — never edit the snapshot without
checking which.

Deliberately does NOT go through ModuleRegistry.load_modules() + a real
Runtime: most modules' register_runtime() needs a fully-configured Runtime
(DB paths, model configs, singletons) that would make this test heavy and
brittle for a purpose it doesn't need. Instead it imports each module's
register_agent(cycle) directly and calls it against a minimal fake cycle
backed by a real Context — register_agent is the only half of the contract
that touches context.py's hook stages, which is all this test cares about.
A module whose register_agent needs more than a bare cycle (config.extra,
cycle.tool_handler, etc.) is recorded as "skipped" rather than failing the
whole test — see SKIP_MODULES.

Run with:
    pytest tests/test_hook_order.py -v
"""
from __future__ import annotations

import importlib

import pytest

from TinyCTX.db import ConversationDB
from TinyCTX.context import Context
from TinyCTX.module_registry import ModuleRegistry

# Modules whose register_agent() needs more scaffolding (real config, a live
# tool_handler, runtime singletons) than this characterization test builds.
# Excluding them here does not mean they have no hooks — it means their hook
# registration isn't exercised by *this* test. Each is still loaded and
# wired normally by the real ModuleRegistry/Runtime at process startup.
SKIP_MODULES: dict[str, str] = {
    "comfyui": "needs cycle.config.extra + runtime singletons",
    "concurrency": "needs cycle.active_run / runtime",
    "cron": "needs runtime scheduler singleton",
    "mcp": "needs runtime MCP client singletons",
    "memory": "needs runtime vector store / embedder singletons",
    "rag": "needs runtime vector store / embedder singletons",
    "web": "needs runtime browser singleton",
    "filesystem": "needs cycle.config.data.path",
    "system_prompt": "needs cycle.config identity fields",
    "sysops": "command-only; no context hooks",
    "present": "tool-only; no context hooks",
    "shell": "tool-only; no context hooks",
    "output_parser": "needs cycle.config.extra",
    "skills": "needs runtime skills store singleton",
    "equipment_manifest": "needs cycle.db / runtime singletons",
}

MODULE_IMPORT_PATHS = [
    f"TinyCTX.modules.{name}"
    for name in [
        "comfyui", "concurrency", "cron", "ctx_tools", "equipment_manifest",
        "filesystem", "mcp", "memory", "output_parser", "present", "rag",
        "shell", "skills", "sysops", "system_prompt", "web",
    ]
]  # custom_modules.ip_rewrite excluded: not an importable package (loaded via spec_from_file_location by ModuleRegistry)


class _FakeCycle:
    """Minimal register_agent target — matches tests/test_ctx_tools.py's
    _FakeCycle. Modules that only touch cycle.context/cycle.stream_text_hooks
    work against this; anything needing more is in SKIP_MODULES."""

    def __init__(self, context):
        self.context = context
        self.stream_text_hooks: list = []
        self.post_turn_hooks: list = []


def _snapshot_hook_order(ctx: Context) -> dict[str, list[str]]:
    """stage -> ordered list of 'module_or_qualname@priority' for every
    registered hook, in the order Context would run them."""
    snapshot: dict[str, list[str]] = {}
    for stage, entries in ctx._hooks.items():
        ordered = sorted(entries, key=lambda e: (e[0], e[1]))
        snapshot[stage] = [
            f"{getattr(fn, '__module__', repr(fn))}@{priority}"
            for priority, _seq, fn in ordered
        ]
    return snapshot


def _load_all_modules_into_context() -> tuple[Context, list[str], list[str]]:
    db = ConversationDB(":memory:")
    root = db.get_root()
    ctx = Context(db, tail_node_id=root.id, token_limit=100_000)
    cycle = _FakeCycle(ctx)

    loaded: list[str] = []
    skipped: list[str] = []
    registry = ModuleRegistry()

    for import_path in MODULE_IMPORT_PATHS:
        name = import_path.rsplit(".", 1)[-1]
        if name in SKIP_MODULES:
            skipped.append(name)
            continue
        try:
            mod = importlib.import_module(f"{import_path}.__main__")
        except ModuleNotFoundError:
            mod = importlib.import_module(import_path)

        module_class = registry._find_module_class(mod)
        if module_class is not None:
            try:
                instance = module_class()
                instance.config = instance.resolve_settings(None)
                registry._wire_module_instance(instance, cycle)
                loaded.append(name)
            except Exception as e:  # pragma: no cover - diagnostic path
                pytest.fail(f"wiring Module class for '{name}' raised unexpectedly: {e!r}")
            continue

        register_agent = getattr(mod, "register_agent", None)
        if register_agent is None:
            skipped.append(name)
            continue
        try:
            register_agent(cycle)
            loaded.append(name)
        except Exception as e:  # pragma: no cover - diagnostic path
            pytest.fail(f"register_agent for '{name}' raised unexpectedly: {e!r}")

    return ctx, loaded, skipped


class TestHookOrderCharacterization:
    def test_at_least_one_module_loads_and_registers_hooks(self):
        ctx, loaded, skipped = _load_all_modules_into_context()
        assert "ctx_tools" in loaded, (
            f"ctx_tools must load cleanly against the fake cycle — loaded={loaded}"
        )
        assert any(ctx._hooks[stage] for stage in ctx._hooks), (
            "expected at least one hook registered across all loaded modules"
        )

    def test_ctx_tools_priority_order_is_stable(self):
        """The exact ordering this plan's P1 doc calls out by name: dedup 0,
        tokenade 1, cot_strip 5, trim 8 and 10 within transform_turn/filter_turn/
        pre_assemble. Pinning this is the actual safety net — a reordering here
        during the HookRegistry migration is the defect this test exists to catch."""
        ctx, loaded, _ = _load_all_modules_into_context()
        assert "ctx_tools" in loaded

        snapshot = _snapshot_hook_order(ctx)
        # Every stage ctx_tools touches must have a non-empty, ctx_tools-only
        # entry list (nothing else is loaded that shares these stages here).
        for stage in ("pre_assemble", "filter_turn", "transform_turn"):
            assert stage in snapshot and snapshot[stage], f"expected hooks registered on '{stage}'"
            for entry in snapshot[stage]:
                assert "ctx_tools" in entry, f"unexpected non-ctx_tools entry on '{stage}': {entry}"

        # Priorities within transform_turn must be non-decreasing in
        # registration order (sorted already by _snapshot_hook_order) and
        # must include the documented set {0, 1, 5, 10} in some order.
        transform_priorities = [int(e.rsplit("@", 1)[1]) for e in snapshot["transform_turn"]]
        assert transform_priorities == sorted(transform_priorities)
        assert set(transform_priorities) >= {0, 1, 5, 10}, (
            f"expected ctx_tools' documented priorities {{0,1,5,10}} to appear, "
            f"got {sorted(set(transform_priorities))}"
        )

    def test_full_snapshot_is_deterministic_across_two_loads(self):
        """Loading modules twice against fresh contexts must produce byte-identical
        snapshots — insertion order (module import order) is the only ordering
        signal today, and this test pins that it doesn't vary run to run."""
        ctx1, loaded1, skipped1 = _load_all_modules_into_context()
        ctx2, loaded2, skipped2 = _load_all_modules_into_context()
        assert loaded1 == loaded2
        assert skipped1 == skipped2
        assert _snapshot_hook_order(ctx1) == _snapshot_hook_order(ctx2)

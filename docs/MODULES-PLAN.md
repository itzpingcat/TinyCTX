# Module, hook and channel rework — plan

Split into three parts, each independently shippable and each depending on
the one before it:

- **[`MODULES-PLAN-P1.md`](MODULES-PLAN-P1.md) — hooks, decorators,
  registration shape.** `HookRegistry`, `HookType`/`Combine`, `@tool`/
  `@hook`/`@command`, the `Module` class, `Scratch`. Fixes the dead
  `register_platform_handler`/`deliver` pair, silently-dead misspelled hook
  stages, ambiguous combine semantics, and per-turn hook re-registration.
  Hook and tool bodies still receive raw `cycle`/`runtime`/`context`
  exactly as they do today — this part only changes how a handler gets
  attached, not what it's handed once it runs.

- **[`MODULES-PLAN-P2.md`](MODULES-PLAN-P2.md) — the AppContext/
  TurnContext facade.** Replaces raw framework-object access with two
  scoped facade objects, closing the reach-through problem (`agent.config`
  40x, `cycle._memory_block`, etc.). A separate, more debatable call than
  Part 1 — trading a smaller API for new vocabulary a module author has to
  learn — evaluated on its own merits once Part 1 has proven itself.

- **[`MODULES-PLAN-P3.md`](MODULES-PLAN-P3.md) — channels.** Renames
  `bridges/` to `channels/` and makes a channel a `Module` that can
  register tools/hooks (platform-scoped) through Part 2's facade, not just
  render output one-way.

Baseline to hold at every step across all three parts: **886 passed,
1 skipped, 0 failed** (the skip is `test_memory_integration.py`, ladybug
engine not installed, pre-existing).

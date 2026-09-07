# Module/hook rework, Part 3 — channels

## Status
Planning only. Nothing here has been implemented. Depends on Part 2
(`MODULES-PLAN-P2.md`) having landed — a channel is a `Module` that
registers tools/hooks through `AppContext`/`TurnContext` like any other
module, so the facade needs to exist and be trustworthy first.

Settled decisions:

- Bridges are renamed **channels** and become modules that own a
  transport.

---

## Problem statement: bridges cannot extend the agent at all

`main.py` scans `bridges/` on a code path separate from `ModuleRegistry`,
imports `TinyCTX.bridges.<name>.__main__`, and starts it with
`mod.run(runtime)`. The only capability channel back into the agent is
`runtime.register_platform_handler` — output rendering, one direction.

A bridge cannot register a tool, a hook, a prompt provider, or per-turn
state. "Discord exposes moderation tools" has nowhere to plug in, and the
workaround — a `modules/discord_tools/` importing the bridge to fish out
its live client — would invert the dependency and deepen the reach-through
problem Part 2 closes.

---

## `Channel`

`TinyCTX/channels/`, replacing `TinyCTX/bridges/`.

```python
class Channel(Module):
    platform:      Platform
    manual_launch: bool = False

    async def run(self, app: AppContext) -> None: ...
    async def render(self, destination: str, event: AgentEvent) -> None: ...
    async def close(self) -> None: ...
```

`Channel.platforms` defaults to `{self.platform.value}`, so a channel's
tools and `TURN_START` hooks apply only to turns from that platform. That
one default is the mechanism the whole exercise is for: the Discord channel
declares `@tool(permissions={Permission.PLATFORM_MODERATE})` on a method
that uses the live `discord.Client` it holds as an instance attribute, and
the tool exists on Discord turns and nowhere else. No global lookup, no
inverted import, no reach-through.

The loader registers `render` on `DELIVER` automatically, retiring
`register_platform_handler` as something modules call by hand.

`main.py`'s bridge-scanning block collapses into "ask the loader for
channels, start the ones with `manual_launch = False`". The
`MANUAL_LAUNCH_ATTR` module-level-flag convention becomes a class
attribute.

### What the rename must not touch

`Platform.DISCORD.value == "discord"` is written into the `platform` key of
every `state_delta` in `agent.db`, and cursor files are named
`data/cursors/discord.json`, `discord_msg_nodes.json`, `cli`. Those are
**persisted data**. The rename changes Python package paths and the
`bridges:` config key; it must leave enum values, session-state values and
cursor filenames exactly as they are, or every existing conversation loses
its platform and every session loses its cursor.

`config.yaml` lives in user instance directories, not the repo. The loader
reads `channels:` and falls back to `bridges:` with a deprecation warning.
Breaking module code in a hard pass is a decision about this repo; silently
breaking a config file on someone's disk is not the same class of thing.

---

## Phases

Baseline to hold: whatever Part 1 and Part 2 leave the suite at, plus their
added tests.

### C0 — Rename only
1. **Rename only, in its own commit**: `bridges/` → `channels/`, ~473
   occurrences across 45 Python files. Scripted, zero behaviour change,
   reviewed as a rename. Do not mix a behaviour change into this commit — a
   diff that large is only reviewable if it is provably mechanical.

Verify: suite green, identical to pre-rename.

### C1 — Channel class and first ports
1. `Channel(Module)` with `platform`, `manual_launch`, `run`, `render`,
   `close`.
2. Port `cli`, `discord`, `telegram`.
3. `main.py`'s scan block collapses into the loader.
4. Config loader reads `channels:`, falls back to `bridges:` with a
   warning.

Verify: all three channels connect and serve a turn end to end.

### C2 — The payoff
1. `discord_channel_info` — read-only, no new permission. Proves platform
   scoping before anything destructive exists.
2. `discord_timeout_user`, `discord_delete_message`, `discord_pin_message`
   behind a new `PLATFORM_MODERATE` capability added to the 17-bool
   `Permission` set.
3. Assert in a test that `discord_*` tools are absent from a CLI turn's
   tool definitions.

Verify: the platform-scoping assertion above passes, plus the moderation
tools function end-to-end on a real (test) Discord channel.

---

## Risks

**Everything here inherits Part 2's facade risk.** A channel's tools and
hooks route through `AppContext`/`TurnContext`; if that facade is missing a
field or has quietly become a passthrough (Part 2's `turn.settings` risk),
channels will be the place it's discovered, since a channel is the module
with the widest legitimate need for framework access (it owns a live
transport client). Don't treat a channel needing "one more thing" from the
facade as a channel-specific special case — treat it as facade feedback.

**C0's diff size is the whole risk of that step.** ~473 occurrences across
45 files is only safe if it's provably mechanical (scripted rename,
reviewed as such) — any hand-edit mixed into that commit defeats the point
of separating it.

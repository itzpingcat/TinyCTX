# Module/hook rework, Part 2 — the AppContext/TurnContext facade

## Status
Planning only. Nothing here has been implemented. Depends on Part 1
(`MODULES-PLAN-P1.md`) having landed — this part assumes `Module`,
`@tool`/`@hook`/`@command`, `HookRegistry`, and `Scratch` already exist and
hook/tool bodies are already reached via decoration rather than
`register_agent`/`register_runtime`. What this part changes is *what a
module is handed once its method runs* — replacing direct `cycle`/
`runtime`/`context`/`agent` access with two facade objects.

This part is deliberately separated from Part 1 because it is a different
kind of decision. Part 1 fixes concrete defects (dead code, silently-dead
hook stages, ambiguous combine semantics, wasteful re-registration) that
are true regardless of how much a module should be allowed to see. This
part is a judgment call about surface area and discoverability, trading a
smaller and more stable API for new vocabulary a module author has to
learn (`turn.scratch`, `turn.state`, namespacing) that doesn't exist in the
"just an object with attributes" mental model OpenLumara-style modules use.
That tradeoff deserves to be evaluated on its own, once Part 1 has shown
whether explicit decoration alone already fixes most of the pain, before
committing to a second, larger abstraction on top of it.

Settled decisions, additional to Part 1's:

- The module-facing surface is a **hard facade**: `AppContext` and
  `TurnContext`, no escape hatch to the raw `AgentCycle`, `Runtime`, or
  `Context`.
- Bridges are renamed **channels** and become modules that own a
  transport — the capability-channel part of this (a channel registering
  tools/hooks through the same facade) belongs here; the `bridges/` →
  `channels/` rename and `Channel(Module)` mechanics are Part 3.

---

## Problem statement

### Modules reach through the object graph

`register_agent(cycle)` (pre-Part-1) or a Part-1-decorated hook/tool body
(post-Part-1) hands each module the whole `AgentCycle`/`Runtime`/`Context`.
Current traversals from `modules/` and `custom_modules/`:

```
agent.config        40x      agent.caller          10x
agent.context       40x      agent.active_run       8x
agent.tool_handler  33x      agent.outbound_events  2x
agent.db            17x
```

plus three reaches into private attributes:

- `cycle._memory_block` (modules/memory:635, 641, 651) — a per-turn value
  parked on the cycle so a post-turn hook and a prompt provider can share
  it.
- `agent._file_read_state` (modules/filesystem:141–144) — same pattern,
  `hasattr` guard included.
- `agent._execute_tool` — referenced only in comments (contracts.py:262,
  modules/filesystem:256, modules/web:581). Never called by a module.

The first two are the scratch problem Part 1 already solves for the
*assembly* and *stream* passes via `Scratch` — but `_memory_block` and
`_file_read_state` are **turn-scoped**, not pass-scoped, and Part 1 has
nowhere for that to live. That gap is what `turn.scratch` closes here. The
third needs nothing — no module calls it.

Why this specifically is worth fixing, not just untidy: none of these 40x/
40x/33x/17x reaches are documented anywhere as "safe to depend on." A
module author copying `modules/memory` today has no way to tell, by reading
`Module`, what's a stable contract and what's an implementation detail that
moves the next time `agent.py` is refactored. The cost isn't that reaching
into `cycle` is sophisticated — it's that the surface is unbounded, so a
typo or a wrong lifetime assumption fails silently (a `hasattr` guard
papering over "is this even set yet") instead of failing at a boundary.

### Bridges cannot extend the agent at all

`main.py` scans `bridges/` on a code path separate from `ModuleRegistry`,
imports `TinyCTX.bridges.<name>.__main__`, and starts it with
`mod.run(runtime)`. The only capability channel back into the agent is
`runtime.register_platform_handler` — output rendering, one direction.

A bridge cannot register a tool, a hook, a prompt provider, or per-turn
state. "Discord exposes moderation tools" has nowhere to plug in, and the
workaround — a `modules/discord_tools/` importing the bridge to fish out
its live client — would invert the dependency and deepen the reach-through
problem. Fixing this requires a channel to be a `Module` registering
through the same facade everything else uses, which is why it's covered
here rather than purely in Part 3's rename.

---

## `AppContext`

Process-scope facade; replaces the `runtime` argument.

```
app.commands    CommandRegistry
app.users       UserStore
app.db          ConversationDB
app.settings    read-only view of global Config
app.data_path   Path
app.workspace   Path
app.push(msg, queue)               -> str
app.deliver(platform, dest, event) -> bool
```

`app.hooks` is absent. Hooks arrive by decoration (Part 1), never by a
module calling something at runtime — keeping registration as the loader's
job, not something a module can do dynamically.

## `TurnContext`

Turn-scope facade; replaces the `cycle` argument. There is no `turn.cycle`.

```
turn.tools      enable(name)  — registration is by decoration (Part 1)
turn.db         ConversationDB
turn.caller     User
turn.env        TurnEnv(platform, agent_name, server_name,
                        channel_name, cursor_key, tail_node_id)
turn.state      namespaced session state — get/set, auto-prefixed by module
turn.scratch    per-turn, per-module dict
turn.settings   read-only view of global Config
turn.run        read-only view of the active Run (id, session_key, intent)
turn.emit(ev)   enqueue an outbound AgentEvent
```

Four of these retire something:

- `turn.env` resolves platform and channel once, centrally. Today
  `modules/cron` (L759–760), `modules/equipment_manifest` (L120, L185) and
  `modules/memory` (L292) each re-derive it from `db.get_state` or
  `load_session_state` with their own defaults.
- `turn.state` enforces the key-namespacing rule that
  `for-contributors/module_template` states as a convention and nothing
  checks.
- `turn.scratch` is where `_memory_block` and `_file_read_state` go. It is
  turn-scoped, distinct from Part 1's pass-scoped assembly and stream
  scratch.
- `turn.emit` replaces `agent.outbound_events.append` (modules/present:148).

**`turn.prompts` is gone from this facade — it doesn't exist here.**
Part 1 already added `@prompt` as a fourth decorator alongside `@tool`/
`@hook`/`@command`, registered once at load like everything else, so
there's no runtime `.register(...)` call left for `TurnContext` to expose.
This is a correction to how the original single-document plan described
this facade (it had `turn.prompts.register(name, provider, *, role,
priority)` as a method call inside a `TURN_START` hook body) — that shape
predated `@prompt` and is superseded by it. A `@prompt`-decorated method's
body takes `ctx` (the assembly `Context`, unchanged) and, when paired with
a feeder hook, `scratch` — not `turn`. If a genuine need for *runtime*
prompt registration turns up during F1/F2 (a provider whose existence, not
just its content, must vary per turn), that would be new facade surface
to design deliberately, not a leftover from the old shape to restore
unexamined.

## `CommandContext`

Replaces the raw `context` dict command handlers get today
(`context["send"]`, `context["runtime"]`, `context["console"]`):

```
ctx.reply(text)   -- the streaming/progress case; return still means "done"
ctx.caller        User
ctx.env           TurnEnv
ctx.app           AppContext
```

Paired with Part 1's already-landed "handlers return instead of calling
send" change — this part only changes the *shape* of the second argument,
not the return contract, which Part 1 already fixed.

---

## Phases

Baseline to hold at every step, same as Part 1: **886 passed, 1 skipped,
0 failed**, plus whatever Part 1 added to the suite (`test_hook_order.py`,
the throughput benchmark, the 50-turn handler-count stability test).

### F0 — Facade skeleton, no migration
1. Add `AppContext`, `TurnContext`, `CommandContext`, `TurnEnv` to
   `TinyCTX/module.py`. Constructed by the loader from the real
   `runtime`/`cycle`/`context` objects Part 1 still hands to hook/tool
   bodies — i.e., the facade wraps, it does not yet replace.
2. Add `turn.scratch` and `turn.state` as real, working namespaced stores.
3. No module changes yet. Verify: 886 green, facade unit-tested in
   isolation against a fake `runtime`/`cycle`.

### F1 — Migrate the same four proving modules
1. `todo`, `ctx_tools`, `shell`, `equipment_manifest` — the same four
   Part 1 proved the decorator shape on — now migrate their hook/tool/
   prompt bodies to take `app`/`turn` instead of `runtime`/`cycle`/
   `context`.
2. `ctx_tools` is again the load-bearing case: its `transform_turn` family
   only ever touched `ctx` (the assembly `Context`, unchanged by this
   part) and `Scratch` (Part 1), so it should need **no facade access at
   all** — if it turns out to need `app` or `turn` for something, that's a
   sign the facade is missing a field, not that ctx_tools was special.
3. `shell`'s callable permission classifier must keep working: confirm it
   receives tool arguments the same way regardless of what the tool body's
   *other* parameters look like.
4. `equipment_manifest`'s `@prompt`/`@hook`-via-scratch pairing (proven in
   Part 1's P2) should also need **no facade access** — its footer's
   `scratch.last_message_ts` read and its `@hook(HookType.PRE_ASSEMBLE)`
   feeder both only touch `ctx`/`scratch`, same as `ctx_tools`. If it turns
   out to need `turn.db` or similar, that's the same signal as #2: a
   missing facade field, not a special case.

Verify: 886 green; existing Part 1 tests unaffected (this part changes
what handlers are *handed*, not how they're *registered* or *combined*).

### F2 — Hard migrate the rest
1. Port the remaining ~17 modules plus `custom_modules/ip_rewrite` (same
   inventory caveat as Part 1: check what's actually in `custom_modules/`
   before starting, since it's gitignored and invisible from the repo).
2. Move `_memory_block` and `_file_read_state` onto `turn.scratch`; delete
   the private attributes and their `hasattr` guards from `AgentCycle`.
3. Delete `outbound_events` from `AgentCycle`; all emission goes through
   `turn.emit`.
4. Convert the 6 `@command`-decorated handlers (Part 1) from the raw
   `context` dict to `CommandContext`.
5. `turn.settings`: start strictly read-only. Each of the ~40 `agent.config`
   reads gets a named accessor on `TurnContext`/`AppContext` as the
   migration finds it — **do not** let `turn.settings` become a generic
   passthrough to `Config`, or the facade re-creates exactly the
   reach-through problem it exists to close. This is the plan's known soft
   spot: track every field promoted this way in the PR description so
   reviewers can see the read-only boundary actually held.

Verify: 886 green; `grep -rn "\.cycle\b\|_memory_block\|_file_read_state\|outbound_events" TinyCTX/modules/`
returns nothing; add a test that `TurnContext.settings` and
`AppContext.settings` are genuinely immutable (attempting a write raises).

---

## Risks

**This is where "beginner friendly" needs an honest answer, not an
assumption.** A facade with ten named-but-novel concepts (`turn.scratch`,
`turn.state`, namespacing, `TurnEnv`) can be *harder* to onboard into than
a familiar, if larger, plain object — at least until the boundaries are
internalized. The facade is justified for framework safety and stability;
it is not automatically justified for beginner-friendliness, and this
document should not claim it is without evidence. **Before F1 ships,
produce the before/after `ctx_tools` comparison (see Part 1's P2 section
for the "before" half) as a real onboarding artifact** — if a newcomer
still can't tell what to do with `turn.scratch` after reading it, that's a
signal to simplify the facade, not to write more docs.

**`turn.settings` is the concrete leak risk**, not a hypothetical one — see
F2 step 5. Every field promoted to a named accessor "as the migration
finds it" is a place discipline can slip; track it explicitly.

**Facade completeness is unproven until F1 lands.** If `ctx_tools`, `todo`,
or `shell` turn out to need something not on `AppContext`/`TurnContext`,
that's real signal about what the facade is missing — surface it as a
facade change, not as a one-off exception that reaches around it.

**Channels depend on this part.** A channel exposing platform-scoped tools
(Part 3) only works cleanly once `TurnContext`/`AppContext` exist — a
channel is a `Module` like any other, and `platforms` gates through the
same facade. Don't start Part 3 before F2 here is green.

---

## Deliberate deferrals

- **Channels** (`bridges/` → `channels/`, `Channel(Module)`, platform
  scoping mechanics) — `MODULES-PLAN-P3.md`.
- Everything Part 1 already deferred (module-defined hook types, per-type
  decorator aliases, background tools, `INBOUND` consumer, MCP-as-modules,
  hot reload) stays deferred here too — this part doesn't reopen any of
  those.

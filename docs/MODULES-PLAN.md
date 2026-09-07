# Module, hook and channel rework — plan

## Status
Planning only. Nothing here has been implemented. This is the plan of
record.

Three decisions are settled and shape everything below: modules migrate to
the new interface in **one hard pass** with no compatibility shim; bridges
are renamed to **channels** and become modules that own a transport; the
module-facing surface is a **hard facade** with no escape hatch to the raw
`AgentCycle`.

---

## Problem statement

### 1. There are seven registries and no shared idea of a hook

| Where | What it registers | Ordering | Error isolation |
|---|---|---|---|
| `context.py::Context._hooks` | 6 stages, `(priority, seq, fn)` sorted | priority | catch-and-skip |
| `agent.py::AgentCycle.post_turn_hooks` (agent.py:40) | async `fn(tail_node_id)` | **insertion only** | none declared |
| `agent.py::AgentCycle.stream_text_hooks` (agent.py:67) | objects with `reset`/`process`/`flush` | **insertion only** | **none, deliberately** — hot path |
| `runtime.py::Runtime._platform_handlers` (runtime.py:100) | one async renderer per platform | n/a | catch-and-log |
| `utils/commands.py::CommandRegistry` | slash commands | n/a | catch-and-log |
| `tool_handling/handler.py::ToolCallHandler` | tools, per cycle | n/a | catch-and-log |
| `module_registry.py::ModuleRegistry._agent_registrations` | `register_agent` callables | **insertion only** | catch-and-log |

Four of the seven are genuinely the same thing — "run these callables at
this point in the lifecycle" — with three different ordering policies and
three different error policies, decided independently.

Two concrete defects follow from the sprawl:

- **`register_platform_handler` and `deliver` are defined twice** in
  `runtime.py`, at L277–321 and again at L328+. The first pair is dead —
  Python keeps the later binding. Neither copy is marked as superseding
  the other.
- **Stage names are unvalidated strings.** `modules/ctx_tools/__main__.py`
  registers with bare literals (`"pre_assemble"`, `"transform_turn"`,
  L81–83, L129–130, L196–197, L249) while `modules/skills`,
  `modules/rag` and `modules/equipment_manifest` import the
  `HOOK_*` constants. `Context.register_hook` appends to a
  `defaultdict(list)`, so a misspelled stage registers successfully into a
  bucket nothing ever drains. The hook silently never fires, forever, with
  no error at registration or at assembly.

### 2. Modules reach through the object graph

`register_agent(cycle)` hands each module the whole `AgentCycle` and
trusts it. Current traversals from `modules/` and `custom_modules/`:

```
agent.config        40x      agent.caller          10x
agent.context       40x      agent.active_run       8x
agent.tool_handler  33x      agent.outbound_events  2x
agent.db            17x
```

plus three reaches into private attributes:

- `cycle._memory_block` (modules/memory:635, 641, 651) — the memory module
  stashes a per-turn value on the cycle so a `post_turn` hook and a prompt
  provider can share it. It is the module's own state, parked on someone
  else's object.
- `agent._file_read_state` (modules/filesystem:141–144) — same pattern,
  `hasattr` guard included.
- `agent._execute_tool` — referenced only in comments (contracts.py:262,
  modules/filesystem:256, modules/web:581). Never actually called by a
  module.

The first two are one missing facility (per-turn per-module scratch
space). The third needs nothing. **The hard facade is cheap** — there is no
legitimate module use of `AgentCycle` internals to design around.

Separately, every module hand-rolls the same config merge:

```python
from TinyCTX.modules.<name> import EXTENSION_META
cfg = EXTENSION_META.get("default_config", {})
if hasattr(cycle.config, "extra") and isinstance(cycle.config.extra, dict):
    cfg = {**cfg, **cycle.config.extra.get("<name>", {})}
```

Eight lines, repeated verbatim in `comfyui`, `concurrency`, `cron`,
`ctx_tools`, `equipment_manifest`, `memory`, `output_parser`, `rag`,
`ip_rewrite`, and documented as the pattern to copy in
`for-contributors/module_template/__main__.py`.

### 3. Bridges cannot extend the agent at all

`main.py` scans `bridges/` on a code path completely separate from
`ModuleRegistry`, imports `TinyCTX.bridges.<name>.__main__`, and starts it
with `mod.run(runtime)`. The only capability channel back into the agent is
`runtime.register_platform_handler` — output rendering, one direction.

A bridge cannot register a tool, a hook, a prompt provider or per-turn
state. So "Discord exposes moderation tools" has nowhere to plug in, and
the workaround — a `modules/discord_tools/` that imports the bridge and
fishes out its live client — would invert the dependency and only deepen
the reach-through problem.

---

## Target shape

Three concepts, one registration path.

**`HookBus`** — one class, one `register`/`emit` pair, declared stages.
Instantiated twice: `app.hooks` (process lifetime) and `turn.hooks` (turn
lifetime). The turn bus chains to the app bus, so a handler registered once
at startup fires on every turn without re-registering.

**`Module`** — a class with declared metadata and lifecycle methods.
Replaces the `register_runtime` / `register_agent` free functions and the
`EXTENSION_META` convention.

**`Channel`** — a `Module` that additionally owns a transport. Replaces
"bridge". A channel's per-turn wiring only fires on turns from its own
platform, which is the entire mechanism for platform-specific tools.

Modules never see `Runtime` or `AgentCycle`. They see `AppContext` and
`TurnContext`.

---

## The hook bus

`TinyCTX/hooks.py`.

### Stages are declared, not strings

```python
@dataclass(frozen=True)
class Stage:
    name:     str
    reducer:  Reducer
    scope:    Scope          # APP | TURN
    isolate:  bool = True    # wrap each handler in try/except
```

Stages live in one frozen table. `register()` on an undeclared stage
raises at registration time, which closes the silent-typo defect.

### Reducers

Five, covering every existing stage. One `emit()` implementation.

| Reducer | Behaviour | Stages |
|---|---|---|
| `FANOUT` | run all, ignore returns | `startup`, `shutdown`, `turn_start`, `pre_assemble`, `pre_assemble_async`, `stream_start`, `post_turn` |
| `VETO` | first falsy return wins, short-circuits | `filter_turn` |
| `CHAIN` | each return feeds the next; `None` means unchanged | `transform_turn`, `post_assemble`, `stream_text`, `inbound` |
| `COLLECT` | gather non-`None` returns into a list | `post_completion`, `stream_end` |
| `DISPATCH` | one handler per key, looked up not iterated | `deliver` |

`emit()` awaits coroutine functions and calls sync ones directly, which
removes the `run_async_hooks` / `run_sync_hooks` split from `Context`.
Ordering is `(priority, registration_seq)` at every stage — `post_turn`
and the stream hooks gain deterministic ordering they do not have today.

### Declared stages

App scope:

- `startup(app)` — replaces `register_runtime`'s body-by-convention
- `shutdown(app)`
- `inbound(msg) -> InboundMessage | None` — rewrite or drop an
  `InboundMessage` before `Runtime.push` writes a node. New; no consumer
  at first, declared because channels are the obvious future user.
- `deliver(destination, event)` — the surviving copy of
  `_platform_handlers`

Turn scope, in firing order:

- `turn_start(turn)` — new; the wiring point that `register_agent`'s
  top-of-function currently is
- `pre_assemble_async(ctx)`
- `pre_assemble(ctx)`
- `filter_turn(entry, age, ctx) -> bool`
- `transform_turn(entry, age, ctx) -> HistoryEntry | None`
- `post_assemble(messages, ctx) -> list[dict] | None`
- `stream_start()` — was `reset()`
- `stream_text(text) -> str` — was `process()`; **`isolate=False`**
- `stream_end() -> str` — was `flush()`
- `post_completion(text, calls, ctx) -> PostCompletionAction | None`
- `post_turn(tail_node_id)`

The stream hooks stop being an object protocol and become three ordinary
stages. Hook state lives in a closure or on the module instance, the same
convention `ctx_tools`' dedup hook already uses.

`stream_text` keeps `isolate=False` deliberately. It runs once per token;
a `try`/`except` per handler per token is not acceptable, and the current
contract — a hook that raises breaks streaming for the cycle — is the
right trade at that frequency. The flag makes the exception explicit
instead of implicit in a comment.

---

## `Module`

`TinyCTX/module.py`.

```python
class Module:
    name:           str
    default_config: dict = {}
    requires:       tuple[str, ...] = ()
    platforms:      frozenset[str] | None = None   # None = every platform

    async def setup(self, app: AppContext) -> None: ...
    def on_turn(self, turn: TurnContext) -> None: ...
    async def teardown(self) -> None: ...
```

A module directory exports `MODULE` — either a `Module` subclass or an
instance of one. The registry instantiates once at startup, calls
`setup` once, and calls `on_turn` per turn for every module whose
`platforms` admits that turn's platform.

`default_config` on the class replaces `EXTENSION_META` and its separate
`__init__.py`. The registry does the merge with `config.extra[name]` once
and hands the result back as `turn.config` / `app.config` — the eight-line
merge disappears from all ten sites.

`requires` gives the registry a topological load order. Today order is
`sorted(dir())` and modules that depend on another module's startup work
rely on alphabetical luck.

### `AppContext`

The process-scope facade. Replaces the `runtime` argument.

```
app.hooks       HookBus (app scope)
app.commands    CommandRegistry
app.users       UserStore
app.db          ConversationDB
app.config      this module's merged config
app.settings    read-only view of global Config (workspace, data, models, ...)
app.data_path   Path
app.workspace   Path
app.push(msg, queue)          -> str
app.deliver(platform, dest, event) -> bool
```

### `TurnContext`

The turn-scope facade. Replaces the `cycle` argument. This is the whole
module-facing surface for per-turn work; there is no `turn.cycle`.

```
turn.tools      register(fn, *, always_on, required_permissions), enable(name)
turn.hooks      HookBus (turn scope, chained to app scope)
turn.prompts    register(name, provider, *, role, priority)
turn.db         ConversationDB
turn.caller     User
turn.env        TurnEnv(platform, agent_name, server_name,
                        channel_name, cursor_key, tail_node_id)
turn.state      namespaced session state — get/set, auto-prefixed by module name
turn.scratch    per-turn, per-module dict
turn.config     this module's merged config
turn.settings   read-only view of global Config
turn.run        read-only view of the active Run (id, session_key, intent)
turn.emit(ev)   enqueue an outbound AgentEvent
```

Four of these retire something:

- `turn.env` resolves platform/channel once, centrally. Today
  `modules/cron` (L759–760), `modules/equipment_manifest` (L120, L185) and
  `modules/memory` (L292) each re-derive it from `db.get_state` or
  `load_session_state` with their own defaults.
- `turn.state` enforces the key-namespacing rule that
  `for-contributors/module_template` currently states as a convention and
  nothing checks.
- `turn.scratch` is where `_memory_block` and `_file_read_state` go.
- `turn.emit` replaces `agent.outbound_events.append` (modules/present:148).

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
`on_turn` fires only on turns originating from that platform. That single
line is the mechanism the whole exercise is for: the Discord channel
registers `discord_timeout_user` in `on_turn`, closing over the live
`discord.Client` it holds as an instance attribute, and the tool exists on
Discord turns and nowhere else. No global lookup, no inverted import, no
reach-through.

The registry auto-registers `render` on the `deliver` stage, which retires
`register_platform_handler` as a thing modules call by hand.

`main.py`'s bridge-scanning block collapses into "ask the registry for
channels, start the ones with `manual_launch = False`". The
`MANUAL_LAUNCH_ATTR` module-level-flag convention becomes a class
attribute.

### What the rename must not touch

`Platform.DISCORD.value == "discord"` is written into the `platform` key
of every `state_delta` in `agent.db`, and cursor files are named
`data/cursors/discord.json`, `discord_msg_nodes.json`, `cli`. Those are
**persisted data**. The rename changes the Python package path and the
`bridges:` config key; it must leave enum values, session-state values and
cursor filenames exactly as they are, or every existing conversation loses
its platform and every session loses its cursor.

`config.yaml` lives in user instance directories, not in the repo. The
loader reads `channels:` and falls back to `bridges:` with a deprecation
warning. Breaking module code in a hard pass is a decision about this
repo; silently breaking a config file on someone's disk is not the same
class of thing.

---

## Phases

Each phase leaves the tree green and is independently shippable. Baseline
to hold at every step: **886 passed, 1 skipped, 0 failed** (the one skip is
`test_memory_integration.py`, ladybug engine not installed, pre-existing).

### P0 — Baseline and cleanup
1. Confirm the baseline → verify: 886/1/0.
2. Delete the dead `register_platform_handler` / `deliver` pair at
   `runtime.py` L277–321 → verify: suite unchanged.
3. Write `tests/test_hook_order.py`: build a cycle with every module
   loaded and snapshot, per stage, the ordered list of registering module
   names → verify: it passes and is committed *before* any bus work.

That characterization test is the safety net for P1. `ctx_tools` relies on
exact priorities (dedup 0, tokenade 1, cot_strip 5, trim 8 and 10) and a
reordering there changes assembled context silently, with no test failure
and no error — it just makes the agent behave slightly differently.

### P1 — The hook bus
1. Add `TinyCTX/hooks.py` — `HookBus`, `Stage` table, five reducers.
2. `Context._hooks` becomes a `HookBus`; `Context.register_hook` and
   `run_async_hooks` / `run_sync_hooks` stay as thin delegating aliases.
3. `post_turn_hooks` and `stream_text_hooks` move onto the bus, with
   list-like proxies left on `AgentCycle` so no module changes yet.
4. `_platform_handlers` becomes the `deliver` stage.

Verify: 886 green; `test_hook_order.py` byte-identical output; new test
that registering an undeclared stage raises.

Every module still uses `register_runtime` / `register_agent`, unchanged.
This phase is invisible from `modules/`.

### P2 — Module class, proven on three
1. Add `TinyCTX/module.py` — `Module`, `AppContext`, `TurnContext`,
   `ToolRegistrar`, namespaced `state`, `scratch`.
2. `ModuleRegistry` gains the class path *alongside* the function path.
   This coexistence is temporary scaffolding for P2 only, not a
   compatibility interface.
3. Migrate three modules chosen to stress different corners: `todo`
   (simplest, proves the shape), `ctx_tools` (heaviest hook user, proves
   ordering and closure state survive), `equipment_manifest` (heaviest
   config user, proves the `default_config` merge).

Verify: 886 green; `test_hook_order.py` identical.

### P3 — Hard migrate
1. Port the remaining ~17 modules plus `custom_modules/ip_rewrite`.
2. Delete the `register_runtime` / `register_agent` path from
   `ModuleRegistry`.
3. Delete `outbound_events`, `_memory_block`, `_file_read_state` from
   `AgentCycle`, and the `post_turn_hooks` / `stream_text_hooks` proxies.
4. Rewrite `for-contributors/module_template/`.

Verify: 886 green; `grep -rn "register_agent\|register_runtime\|EXTENSION_META" TinyCTX/`
returns nothing outside docs.

**Before starting P3, inventory `custom_modules/`.** It is gitignored, so
only `ip_rewrite` is visible from the repo. Anything else living there is
invisible to this plan and breaks at step 2 with no warning.

### P4 — Channels
1. **Rename only, in its own commit**: `bridges/` → `channels/`, ~473
   occurrences across 45 Python files. Scripted, zero behaviour change,
   reviewed as a rename. Do not mix a single behaviour change into this
   commit — a rename diff that large is only reviewable if it is provably
   mechanical.
2. `Channel(Module)` with `platform`, `manual_launch`, `run`, `render`,
   `close`. Port `cli`, `discord`, `telegram`.
3. `main.py`'s scan block collapses into the registry.
4. Config loader reads `channels:`, falls back to `bridges:` with a
   warning.

Verify after step 1 alone: 886 green. After step 4: all three channels
connect and serve a turn end to end.

### P5 — The payoff
1. `discord_channel_info` — read-only, no new permission. Proves the
   platform scoping works before anything destructive exists.
2. `discord_timeout_user`, `discord_delete_message`, `discord_pin_message`
   behind a new `PLATFORM_MODERATE` capability added to the 17-bool
   `Permission` set.
3. Assert in a test that `discord_*` tools are absent from a CLI turn's
   tool definitions.

---

## Risks

**Ordering changes are silent.** The only defence is `test_hook_order.py`
existing before P1 starts. Nothing else in the suite would catch a
reordering inside `transform_turn`.

**`stream_text` is a per-token path.** If the bus wraps it in the generic
isolated `emit`, throughput drops on every response. The `isolate=False`
flag has to be honoured by `emit`'s fast path, and that needs its own
test.

**P3 is one large diff by construction.** That is the cost of the hard
migrate. P2 is what makes it safe: by the time P3 starts, the shape has
been proven against the two hardest modules, so P3 is mechanical rather
than exploratory.

**`Platform` values and cursor filenames are persisted.** See the rename
section above. A `sed -i 's/bridge/channel/g'` over the tree corrupts
live instance data.

**`turn.settings` is a leak risk.** It exists because 40 `agent.config`
reads have to go somewhere, and most want `workspace.path`, `data.path`
or `models`. If it ends up as a mutable passthrough to `Config` it
re-creates the reach-through the facade is meant to close. It is
read-only, and each field a module actually uses gets promoted to a named
accessor as the migration finds it.

---

## Deliberate deferrals

- **Registering stateless per-turn hooks once at startup.** The turn bus
  chaining to the app bus makes this possible, and it would stop ~20
  closures being rebuilt every turn. Deferred: it changes when closures
  capture, and mixing that with the migration would make P3 unreviewable.
  Revisit after P3, one module at a time.
- **`inbound` stage has no consumer.** Declared in P1 because it is
  obviously where a channel wants to normalise a message, but nothing
  uses it until something asks.
- **`CommandRegistry` stays separate.** Slash commands carry permission
  declarations, a dispatch/introspection path and a startup assertion that
  hooks do not have. Folding them into the bus would mean either
  weakening that or specialising the bus for one stage.
- **MCP servers as modules.** `modules/mcp` currently registers a
  server's tools into the cycle by hand. It could become a module that
  spawns child modules once `requires` and topological load order exist.
  Not in this pass.
- **Hot reload.** A `Module` instance with `setup`/`teardown` makes
  reloading a module without restarting the process tractable for the
  first time. Explicitly out of scope; noted so the lifecycle methods are
  not later mistaken for accidental symmetry.

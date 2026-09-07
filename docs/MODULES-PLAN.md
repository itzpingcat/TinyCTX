# Module, hook and channel rework — plan

## Status
Planning only. Nothing here has been implemented. This is the plan of
record.

The goal is that writing a module requires knowing almost nothing about
TinyCTX's internals. A module author should be able to open an existing
module, see everything the framework knows about it on the line above each
method, and copy the shape.

Settled decisions, which shape everything below:

- Every framework attachment point is an **explicit decorator**. Nothing is
  wired by naming convention, method-name matching, or "public methods are
  tools". Untagged methods are plain helpers, invisible to the framework.
- There are exactly **three decorators**: `@tool`, `@hook`, `@command`.
- Hooks live in **one flat `HookRegistry`**. No app/turn scope split.
- A `HookType` is an enum member that **carries its own combine strategy**.
  The enum is closed.
- Hooks are **stateless and registered once per process lifetime**.
  Per-pass working data lives in a scratch namespace supplied by `emit`.
- Modules migrate in **one hard pass**, no compatibility shim.
- Bridges are renamed **channels** and become modules that own a transport.
- The module-facing surface is a **hard facade**: `AppContext` and
  `TurnContext`, no escape hatch to the raw `AgentCycle`.

---

## Problem statement

### 1. Four registries are the same idea with different rules

| Where | Registers | Ordering | Error isolation |
|---|---|---|---|
| `context.py::Context._hooks` | 6 stages, `(priority, seq, fn)` | priority | catch-and-skip |
| `agent.py::AgentCycle.post_turn_hooks` (agent.py:40) | async `fn(tail_node_id)` | **insertion only** | none declared |
| `agent.py::AgentCycle.stream_text_hooks` (agent.py:67) | objects with `reset`/`process`/`flush` | **insertion only** | **none, deliberately** |
| `runtime.py::Runtime._platform_handlers` (runtime.py:100) | one async renderer per platform | n/a | catch-and-log |

Same idea — "run these callables at this point" — with three ordering
policies and three error policies, each decided in isolation.

Two concrete defects follow:

- **`register_platform_handler` and `deliver` are defined twice** in
  `runtime.py`, at L277–321 and again at L328+. The first pair is dead —
  Python keeps the later binding.
- **Stage names are unvalidated strings.** `modules/ctx_tools` registers
  with bare literals (`"pre_assemble"`, `"transform_turn"`; L81–83,
  L129–130, L196–197, L249) while `modules/skills`, `modules/rag` and
  `modules/equipment_manifest` import the `HOOK_*` constants.
  `Context.register_hook` appends into a `defaultdict(list)`, so a
  misspelled stage registers successfully into a bucket nothing drains.
  The hook silently never fires, forever, with no error at registration or
  at assembly.

### 2. Combining logic lives at the call site, not the type

`context.py` iterates each stage's bucket itself, and each loop does
something different with the return values — veto for `filter_turn`, chain
for `transform_turn` and `post_assemble`, collect for `post_completion`,
ignore for `pre_assemble`. Two callers of one stage could disagree about
how returns combine and nothing would catch it. The rule belongs to the
type.

### 3. Hooks are re-registered every turn for no reason

`ModuleRegistry.register_agent(cycle)` runs on every `AgentCycle` and each
module rebuilds its closures from scratch. `ctx_tools` alone constructs
four hook closures plus a `_LabelPrefixStripHook` per turn.

This does not leak today, because `Context` is constructed fresh in
`AgentCycle.run()` (agent.py:167) and `Context._hooks` dies with the turn.
It is waste, not a defect — but it *forces* awkward code, and it becomes a
real leak the moment the registry outlives the turn.

The forced awkwardness is visible in `ctx_tools`: `last_user_idx: list[int] = [-1]`
is a one-element list existing solely to give a nested function a mutable
cell. `_register_tokenade` rebuilds a `tiktoken` encoding per turn because
there is nowhere with the right lifetime to keep it.

**No hook in the codebase holds state across turns.** Every apparent piece
of state is derived data with a single-pass lifetime, cleared and
recomputed at the top of that pass:

| Module | Data | Recomputed in | Lifetime |
|---|---|---|---|
| ctx_tools dedup | `suppressed_tool`, `suppressed_calls` | `pre_assemble` (clears both) | one assemble |
| ctx_tools cot_strip | `last_user_idx` | `pre_assemble` | one assemble |
| ctx_tools trim | `trimmed_calls` | `pre_assemble` (clears) | one assemble |
| ctx_tools label_prefix | `_buf`, `_resolved` | `reset()` | one model attempt |
| ctx_tools tokenade | `_enc` | never — pure cache | wants process lifetime |

A hook is a stateless mutator: inputs and a context in, outputs out. Its
working data belongs to the pass. `_enc` is the one thing that genuinely
wants a longer life, and it is a cache — which module-instance lifetime
gives away free.

Scratch is *not* session state. The DB `state_delta` is for data that must
outlive the turn and replay from the conversation tree. `suppressed_tool`
is derived from the assembled dialogue and meaningless outside the pass
that built it; persisting it would write per-pass scratch into
conversation history.

### 4. Modules reach through the object graph

`register_agent(cycle)` hands each module the whole `AgentCycle`. Current
traversals from `modules/` and `custom_modules/`:

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

The first two are the scratch problem again. The third needs nothing.
**The hard facade is cheap** — there is no legitimate module use of
`AgentCycle` internals to design around.

### 5. Boilerplate per module

Every module hand-rolls the same config merge:

```python
from TinyCTX.modules.<name> import EXTENSION_META
cfg = EXTENSION_META.get("default_config", {})
if hasattr(cycle.config, "extra") and isinstance(cycle.config.extra, dict):
    cfg = {**cfg, **cycle.config.extra.get("<name>", {})}
```

Repeated verbatim in `comfyui`, `concurrency`, `cron`, `ctx_tools`,
`equipment_manifest`, `memory`, `output_parser`, `rag`, `ip_rewrite`, and
documented as the pattern to copy in `for-contributors/module_template/`.

On top of that, a module that wants a tool, a hook and a command writes
three different registration calls into two different lifecycle functions,
against three registries with different argument shapes.

### 6. Bridges cannot extend the agent at all

`main.py` scans `bridges/` on a code path separate from `ModuleRegistry`,
imports `TinyCTX.bridges.<name>.__main__`, and starts it with
`mod.run(runtime)`. The only capability channel back into the agent is
`runtime.register_platform_handler` — output rendering, one direction.

A bridge cannot register a tool, a hook, a prompt provider, or per-turn
state. "Discord exposes moderation tools" has nowhere to plug in, and the
workaround — a `modules/discord_tools/` importing the bridge to fish out
its live client — would invert the dependency and deepen the reach-through
problem.

---

## The module interface

One import, three decorators, no registration calls.

```python
from TinyCTX import Module, tool, hook, command, HookType, Permission


class Notes(Module):
    """Lets the agent store notes."""

    settings = {
        "max_note_chars": {
            "default": 8000,
            "type": "int",
            "description": "Longest note the agent may write.",
        },
    }

    @hook(HookType.STARTUP)
    async def open_store(self, app):
        self.store = NoteStore(app.data_path / "notes")

    @tool(permissions={Permission.FILE_WRITE})
    async def note_create(self, name: str, category: str, content: str):
        """Create a new note.

        Args:
            name: filename for the note
            category: folder to file it under
        """
        if len(content) > self.config["max_note_chars"]:
            return self.error("note too long")
        self.store.write(category, name, content)
        return self.ok("note created")

    @command("notes", "list", permissions=None, help="List stored notes")
    async def cmd_list(self, args, ctx):
        await ctx.reply("\n".join(self.store.categories()))

    def _slug(self, name):          # untagged — framework never sees it
        return name.lower().strip()
```

Everything the framework knows is on the line above the method. Nothing
else in the class is reachable by the LLM, the hook registry, or the
command dispatcher.

### `@tool`

```python
def tool(*, permissions, listing_permissions=None,
         always_on=False, name=None): ...
```

Carries the existing `ToolCallHandler.register_tool` contract through
unchanged. `permissions` accepts all three current forms:

- a `set[Permission]` — static
- `None` — explicitly ungated
- a **callable** — evaluated per call against the tool's own arguments

The callable form is load-bearing and must not be simplified away.
`modules/shell` classifies by parsing the bash it was handed
(`perms.py::required_permissions_for_shell(command, timeout, backend_access, **_ignored)`),
so `ls` and `rm -rf /` are the same tool with different permission sets;
`modules/present` adds `ROOT` only for a solo system-file request.
`listing_permissions` stays as the worst-case set used when filtering the
tool list under `minimal_tokens`, where no arguments exist yet.

Omitting `permissions=` is a `TypeError` at class definition — it is a
required keyword argument. `assert_permissions_declared()` stays anyway,
to catch tools built dynamically rather than by decoration.

A classifier may be an imported function (as `shell`'s is today — it lives
next to the 500-line policy loader and belongs there) or a method on the
module. If it is a method, the loader passes the **bound** method so
`self` is available.

Schema generation is unchanged: signature plus Google-style docstring, as
`ToolCallHandler` already does. Tool name defaults to the method name, and
the registry raises on a collision at load rather than resolving by prefix.

### `@hook`

```python
def hook(type: HookType, *, priority: int = 0): ...
```

One decorator, the type as data. `HookType.` autocompletes to the full set
in an editor, which is how a module author discovers what stages exist
without leaving the file; a typo is an `AttributeError` on a known enum
rather than a silent miss.

Multiple methods on one class may tag the same type at different
priorities. This is required, not incidental: `ctx_tools` registers three
separate `transform_turn` handlers at priorities 0, 5 and 10, and they
become three tagged methods.

### `@command`

```python
def command(namespace: str, sub: str = "", *, permissions,
            help: str = "", params: ParamSpec | None = None): ...
```

Wraps `CommandRegistry.register` with its existing semantics intact.
Commands are **not** tools and keep their own shape:

- Handler signature is `(args: list[str], ctx)` — a token list and a
  dispatch context — not typed keyword arguments.
- `namespace` / `sub` is the two-word grammar `/memory consolidate`
  parses against; `sub=""` handles bare `/namespace`.
- `params` is a separate `list[(name, type, description)]` used by channels
  to build native typed slash commands (Discord). It is not derived from
  the handler signature, because the handler does not have one to derive
  from.
- `permissions` is a required keyword, same `_UNSET`-vs-`None` distinction
  as tools, still asserted at startup.

Registration is process-scope: commands are registered once at load, as
they are today.

One change to the handler's second argument. Today it is a raw dict the
channel assembles, and handlers reach into it for `context["send"]`,
`context["runtime"]`, `context["console"]`. It becomes a `CommandContext`
object exposing `reply()`, `caller`, `env` and `app`, so a command handler
gets the same facade guarantee as everything else.

### `settings`

A declarative schema, not a flat defaults dict:

```python
settings = {
    "trim_thinking": {
        "default": "auto",
        "type": "select",
        "options": {"all": "...", "auto": "...", "none": "..."},
        "description": "How much <think> survives into later turns.",
    },
}
```

Merged once by the loader with `config.extra[<module name>]` and exposed
as `self.config` — a plain mapping of resolved values. The eight-line
merge disappears from all ten sites, and `EXTENSION_META` and its separate
`__init__.py` go with it.

The schema buys three things a bare dict cannot: `scripts/generate_config_example.py`
can emit `example.config.yaml` with real descriptions instead of guessing;
values are validated at load with a useful error rather than a `KeyError`
mid-turn; and a settings UI (a channel's `/config`, or anything later) can
render it without hardcoding per-module knowledge.

### `dependencies`

```python
dependencies = ["ddgs", "playwright"]
```

Declaration only. The loader checks whether each import resolves and, if
not, **skips the module with a clear log line** instead of failing at
import with a stack trace — which is what turns one missing optional
package into a dead framework today.

TinyCTX **never installs anything at runtime.** It ships as a prebuilt
Docker image; a container that pip-installs on boot drifts from its image
and breaks reproducibility. Dependencies belong in `requirements.txt` and
the image build. This field exists solely so a module whose optional dep is
absent degrades to "not loaded" rather than "crashes the process".

### `self.ok()` / `self.error()`

Tools return a uniform envelope — `{"status": "success"|"error", "content": ...}` —
so the model sees one shape across every module instead of each tool
inventing its own error convention. Existing tools returning bare strings
are wrapped during migration.

### Lifecycle

There are no mandatory lifecycle methods. Startup, shutdown and background
work are hook types like any other:

- `@hook(HookType.STARTUP)` — replaces `register_runtime`
- `@hook(HookType.SHUTDOWN)` — cleanup
- `@hook(HookType.BACKGROUND)` — the loader starts it as an `asyncio.Task`
  and cancels it on shutdown. `cron`, `heartbeat` and memory's librarian
  loops each hand-roll `create_task` plus their own cancellation today.
- `@hook(HookType.TURN_START)` — per-turn wiring: enabling a tool for this
  turn, registering a prompt provider bound to this turn's data.

A module with only tools defines no hooks at all.

---

## `HookRegistry`

`TinyCTX/hooks.py`.

One registry for the process: a `dict[HookType, list[Handler]]` that a
caller looks up and runs inline, on its own stack, with returns feeding
back into the caller's next line. It is not a bus — no queue, no
decoupling, no deferred delivery. `Context.assemble()` calls
`TRANSFORM_TURN` and waits, because it needs the result.

```python
class HookRegistry:
    def register(self, type: HookType, fn, *, priority: int = 0) -> None: ...
    def emit(self, type: HookType, *args, scratch: Scratch) -> Any: ...
```

Modules do not call `register` — the loader does, once, walking each
class's tagged methods. `register` on a non-`HookType` raises. Ordering is
`(priority, registration_seq)` at every type, so `post_turn` and the
stream types gain determinism they lack today. `emit` awaits coroutine
handlers and calls sync ones directly, removing the
`run_async_hooks`/`run_sync_hooks` split from `Context`.

There is no scope field. The caller already knows which type it wants —
`Context.assemble()` pulls `TRANSFORM_TURN`, `Runtime.push()` pulls
`INBOUND` — so a scope on the type would restate at declaration what is
implicit at every call site.

### The type carries its combine strategy

```python
class Combine(Enum):
    FANOUT   = auto()   # run all, ignore returns
    VETO     = auto()   # first falsy return wins, short-circuits
    CHAIN    = auto()   # each return feeds the next; None means unchanged
    COLLECT  = auto()   # gather non-None returns into a list
    DISPATCH = auto()   # one handler per key, looked up not iterated


class HookType(Enum):
    #                    wire name           combine           isolate
    STARTUP         = ("startup",         Combine.FANOUT)
    SHUTDOWN        = ("shutdown",        Combine.FANOUT)
    BACKGROUND      = ("background",      Combine.FANOUT)
    INBOUND         = ("inbound",         Combine.CHAIN)
    DELIVER         = ("deliver",         Combine.DISPATCH)

    TURN_START      = ("turn_start",      Combine.FANOUT)
    PRE_ASSEMBLE    = ("pre_assemble",    Combine.FANOUT)
    FILTER_TURN     = ("filter_turn",     Combine.VETO)
    TRANSFORM_TURN  = ("transform_turn",  Combine.CHAIN)
    POST_ASSEMBLE   = ("post_assemble",   Combine.CHAIN)
    STREAM_START    = ("stream_start",    Combine.FANOUT)
    STREAM_TEXT     = ("stream_text",     Combine.CHAIN,  False)
    STREAM_END      = ("stream_end",      Combine.COLLECT)
    POST_COMPLETION = ("post_completion", Combine.COLLECT)
    POST_TURN       = ("post_turn",       Combine.FANOUT)
```

The member is the declaration. `emit()` needs no argument telling it how to
behave, and two callers of one type cannot disagree about combining.

`PRE_ASSEMBLE_ASYNC` is gone as a separate type: `emit` awaiting coroutines
makes sync-vs-async a property of the handler, not of the stage.

The enum is closed. A module cannot define a new type without editing core
— an open registry would let a typo'd custom type silently create a second
bucket, which is the defect being fixed.

### Isolation

Every type wraps handlers in try/except except `STREAM_TEXT`, which carries
`isolate=False`. It runs once per token; a try/except per handler per token
is not acceptable, and the current contract — a raising hook breaks
streaming for the cycle — is the right trade at that frequency. `emit` must
honour this on a fast path with no wrapper allocation, and that needs its
own test.

### Scratch

Handlers are registered once and shared across every turn, so they cannot
close over per-pass data. `emit` supplies a `Scratch` — a namespaced
mapping created at the top of a pass and dropped at the bottom:

```python
@hook(HookType.TRANSFORM_TURN, priority=0)
async def dedup(self, entry, age, ctx, scratch):
    if entry.role == "tool" and entry.index in scratch.suppressed:
        return False
```

Namespacing is per module, so one module's handler groups must use distinct
keys; the migration must not flatten `ctx_tools`' dedup `suppressed` and
trim `trimmed` into one name.

Two passes, two lifetimes:

- **Assembly pass** — created at the top of `Context.assemble()`, dropped
  at the bottom. Spans `PRE_ASSEMBLE` → `FILTER_TURN` → `TRANSFORM_TURN` →
  `POST_ASSEMBLE`. This is what `suppressed_tool`, `last_user_idx` and
  `trimmed_calls` need, and what lets `PRE_ASSEMBLE` hand data to
  `FILTER_TURN`.
- **Stream pass** — created at each `STREAM_START`, dropped after
  `STREAM_END`. Spans one model attempt in `_stream_inference`'s
  `for model_name in model_chain` loop, exactly where
  `_LabelPrefixStripHook.reset()` is called today (agent.py:509).

`_LabelPrefixStripHook` therefore stops being an object protocol and
becomes three tagged methods over stream scratch. `tokenade`'s `_enc` moves
to the module instance, where a process-lifetime cache belongs.

---

## `Module`

`TinyCTX/module.py`.

```python
class Module:
    name:         str                        # defaults to snake_case class name
    settings:     dict = {}
    dependencies: tuple[str, ...] = ()
    requires:     tuple[str, ...] = ()       # other modules, for load order
    platforms:    frozenset[str] | None = None   # None = every platform
    unsafe:       bool = False               # flagged in settings UIs
```

A module directory (or single `.py` file) exports one `Module` subclass.
The loader instantiates it once, walks its tagged methods, and registers
them.

`requires` gives a topological load order. Today order is `sorted(dir())`
and modules depending on another's startup work rely on alphabetical luck.

`platforms` gates `TURN_START` and tool registration to matching turns —
the mechanism channels use.

### `AppContext`

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

`app.hooks` is absent. Hooks arrive by decoration.

### `TurnContext`

Turn-scope facade; replaces the `cycle` argument. There is no `turn.cycle`.

```
turn.tools      enable(name)  — registration is by decoration
turn.prompts    register(name, provider, *, role, priority)
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
  turn-scoped, distinct from the pass-scoped assembly and stream scratch.
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

Each phase leaves the tree green and is independently shippable. Baseline
to hold at every step: **886 passed, 1 skipped, 0 failed** (the skip is
`test_memory_integration.py`, ladybug engine not installed, pre-existing).

### P0 — Baseline and cleanup
1. Confirm the baseline → verify: 886/1/0.
2. Delete the dead `register_platform_handler` / `deliver` pair at
   `runtime.py` L277–321 → verify: suite unchanged.
3. Write `tests/test_hook_order.py`: build a cycle with every module loaded
   and snapshot, per type, the ordered list of registering module names →
   verify: passes, committed *before* any registry work.

That characterization test is the safety net for P1. `ctx_tools` depends on
exact priorities (dedup 0, tokenade 1, cot_strip 5, trim 8 and 10). A
reordering changes assembled context silently — no test failure, no error,
just slightly different agent behaviour.

### P1 — HookRegistry
1. Add `TinyCTX/hooks.py` — `HookRegistry`, `HookType`, `Combine`,
   `Scratch`.
2. `Context` emits through it; `register_hook`, `run_async_hooks` and
   `run_sync_hooks` stay as thin delegating aliases so no module changes.
3. `post_turn_hooks` and `stream_text_hooks` move onto it, with list-like
   proxies left on `AgentCycle`.
4. `_platform_handlers` becomes `DELIVER`.

Registration stays per-turn here — modules still call `register_agent`.
Moving to process-lifetime registration is P3, once modules are classes
with tagged methods to register from.

Verify: 886 green; `test_hook_order.py` byte-identical; new tests that a
non-`HookType` raises and that `STREAM_TEXT` takes the no-wrapper path.

### P2 — Decorators and Module class, proven on three
1. Add `TinyCTX/module.py` — `Module`, `AppContext`, `TurnContext`,
   `CommandContext`, `Scratch` namespacing, settings-schema merge.
2. Add `TinyCTX/decorators.py` — `@tool`, `@hook`, `@command`.
3. The loader gains the class path alongside the function path. Temporary
   scaffolding for P2 only, not a compatibility interface.
4. Migrate three modules chosen to stress different corners: `todo`
   (simplest, proves the shape), `ctx_tools` (heaviest hook user, proves
   scratch replaces closure state and that same-type-different-priority
   tagging works), `shell` (proves `@tool` carries a callable classifier
   and `listing_permissions` intact).

`shell` is the one that decides whether `@tool` is sufficient. If its
`required_permissions_for_shell` classifier survives decoration unchanged,
every other tool will.

Verify: 886 green; `test_hook_order.py` identical.

### P3 — Hard migrate
1. Port the remaining ~17 modules plus `custom_modules/ip_rewrite`.
2. All hook registration moves to decoration. The registry becomes
   process-lifetime.
3. Migrate the 6 `commands.register()` call sites (runtime.py:257/263/267,
   memory:435/448, sysops:174) to `@command`, and convert the `context`
   dict to `CommandContext`.
4. Delete `register_runtime`/`register_agent` from the loader, and
   `EXTENSION_META` throughout.
5. Delete `outbound_events`, `_memory_block`, `_file_read_state` from
   `AgentCycle`, and the `post_turn_hooks`/`stream_text_hooks` proxies.
6. Rewrite `for-contributors/module_template/` as one annotated file.

Verify: 886 green; `grep -rn "register_agent\|register_runtime\|EXTENSION_META" TinyCTX/`
returns nothing outside docs. Add a test asserting the handler count for a
given type is stable across 50 consecutive turns — the direct regression
test for re-registration creeping back.

**Before starting P3, inventory `custom_modules/`.** It is gitignored, so
only `ip_rewrite` is visible from the repo. Anything else living there is
invisible to this plan and breaks at step 4 with no warning.

### P4 — Channels
1. **Rename only, in its own commit**: `bridges/` → `channels/`, ~473
   occurrences across 45 Python files. Scripted, zero behaviour change,
   reviewed as a rename. Do not mix a behaviour change into this commit — a
   diff that large is only reviewable if it is provably mechanical.
2. `Channel(Module)` with `platform`, `manual_launch`, `run`, `render`,
   `close`. Port `cli`, `discord`, `telegram`.
3. `main.py`'s scan block collapses into the loader.
4. Config loader reads `channels:`, falls back to `bridges:` with a
   warning.

Verify after step 1 alone: 886 green. After step 4: all three channels
connect and serve a turn end to end.

### P5 — The payoff
1. `discord_channel_info` — read-only, no new permission. Proves platform
   scoping before anything destructive exists.
2. `discord_timeout_user`, `discord_delete_message`, `discord_pin_message`
   behind a new `PLATFORM_MODERATE` capability added to the 17-bool
   `Permission` set.
3. Assert in a test that `discord_*` tools are absent from a CLI turn's
   tool definitions.

---

## Risks

**Ordering changes are silent.** The only defence is `test_hook_order.py`
existing before P1 starts. Nothing else in the suite catches a reordering
inside `TRANSFORM_TURN`.

**`STREAM_TEXT` is a per-token path.** If `emit` wraps it in the generic
isolated path, throughput drops on every response. `isolate=False` needs a
fast path with no wrapper allocation and its own test.

**Decorators run at class-definition time**, before any instance exists.
They can only record metadata onto the function object; actual registration
happens when the loader walks the instantiated class. A decorator that
tries to touch `self`, config or the app will fail at import.

**Callable permission classifiers must keep receiving tool arguments by
name.** `required_permissions_for_shell(command, timeout, backend_access, **_ignored)`
is called as `required_fn(**args)`. If `@tool` ever reshapes a tool's
signature, `shell` and `present` break in a way that fails *open* on a
`TypeError` path unless the handler's existing catch keeps worst-casing.
Verify that path explicitly in P2.

**Command handlers are not tools** and must not be unified with them.
Different signature (`(args, ctx)` vs typed kwargs), different registry,
separate `params` for native slash commands, dispatch by two-word grammar.
`@command` wraps the existing registry; it does not merge it.

**Scratch namespacing is a correctness boundary.** `ctx_tools` runs three
handler groups with separate working sets. Per-module namespacing with
distinct keys is fine; flattening them is not.

**P3 is one large diff by construction.** That is the cost of the hard
migrate. P2 is what makes it safe: by the time P3 starts the shape has been
proven against the two hardest modules.

**`turn.settings` is a leak risk.** It exists because 40 `agent.config`
reads have to go somewhere, and most want `workspace.path`, `data.path` or
`models`. If it becomes a mutable passthrough to `Config` it re-creates the
reach-through the facade closes. It is read-only, and each field a module
actually uses gets promoted to a named accessor as the migration finds it.

---

## Deliberate deferrals

- **Module-defined hook types.** The enum is closed. An open
  `register_type` would let a typo'd custom type silently create a second
  bucket — the defect being fixed.
- **Per-type hook decorator aliases** (`@transform_turn(priority=10)`).
  Nicer at the call site, but a second set of names parallel to `HookType`
  that can drift. If wanted later, generate them from the enum so they
  cannot.
- **A tool-call timeout.** Nothing bounds a tool's execution today; a hung
  `open_url` or MCP call blocks the cycle indefinitely. Unrelated to this
  refactor; worth its own issue.
- **`INBOUND` has no consumer.** Declared in P1 because it is obviously
  where a channel wants to normalise or drop a message before it becomes a
  node, but nothing uses it until something asks.
- **MCP servers as modules.** `modules/mcp` registers a server's tools into
  the cycle by hand. It could become a module spawning child modules once
  `requires` and topological load order exist.
- **Hot reload.** A `Module` instance with tagged lifecycle hooks, and
  hooks registered once rather than per turn, makes reloading a module
  without restarting the process tractable for the first time. Out of scope
  here; noted so the lifecycle hooks are not later mistaken for accidental
  symmetry.

# Module/hook rework, Part 1 — hooks, decorators, registration shape

## Status
Planning only. Nothing here has been implemented. This is the plan of
record for Part 1. Part 2 (`MODULES-PLAN-P2.md`) covers the `AppContext`/
`TurnContext` facade that replaces raw `cycle`/`runtime`/`context` access.
Part 3 (`MODULES-PLAN-P3.md`) covers channels.

**This split exists because the facade (Part 2) is a separable, higher-risk
abstraction layer on top of a registration-shape fix (this part) that is
independently justified. Part 1 alone fixes real defects — the dead
`register_platform_handler`/`deliver` pair, silently-dead misspelled hook
stages, ambiguous combine semantics, and per-turn hook re-registration —
without deciding anything about how much a module should be allowed to
see. Modules written against Part 1 still receive `cycle`, `runtime`, and
`context` directly, same as today. Part 2 is a separate call the project
can make later, on its own merits, once Part 1 has proven itself in
practice.**

Settled decisions for this part:

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
- **Hook and tool bodies still receive raw framework objects** —
  `cycle`, `runtime`, `context`, `agent` — exactly as they do today.
  Nothing in this part restricts what a module can reach at call time.
  Only *registration* changes: how a handler gets attached, not what
  it's handed once it runs.

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

### 4. Boilerplate per module

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

**Note:** the reach-through problems from the plan's original section 4
("Modules reach through the object graph" — `agent.config` 40x,
`agent.context` 40x, `cycle._memory_block`, `agent._file_read_state`, etc.)
and the bridges/channels capability gap are **not addressed in this part**.
They are real, but fixing them means deciding what a module is allowed to
see — that decision, and its facade, is Part 2 and Part 3's job. This part
only fixes *how a handler gets attached*, not *what it's handed once
attached*. Modules migrated under this part keep reaching into `cycle`/
`agent`/`runtime` exactly as before.

---

## The module interface

One import, three decorators, no registration calls — but hook and tool
bodies still take the raw framework objects they take today.

```python
from TinyCTX import Module, tool, hook, command, HookType, Permission, ToolError


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
    async def open_store(self, runtime):
        # `runtime` is the same Runtime object modules get today —
        # no facade yet. Reach into it exactly as register_runtime() does now.
        self.store = NoteStore(runtime.data_path / "notes")

    @tool(permissions={Permission.FILE_WRITE})
    async def note_create(self, name: str, category: str, content: str):
        """Create a new note.

        Args:
            name: filename for the note
            category: folder to file it under
        """
        if len(content) > self.config["max_note_chars"]:
            raise ToolError("note too long")
        self.store.write(category, name, content)
        return "note created"

    @command("notes", "list", permissions=None, help="List stored notes")
    async def cmd_list(self, args, context):
        # `context` is still the raw dict channels assemble today.
        return "\n".join(self.store.categories())

    def _slug(self, name):          # untagged — framework never sees it
        return name.lower().strip()
```

Everything the framework knows about *where a method attaches* is on the
line above it. What the method is handed once it runs is unchanged from
today — this part does not add or remove arguments to hook/tool/command
bodies beyond what registration already implies (e.g. a `TRANSFORM_TURN`
hook still gets `(entry, age, ctx)`, where `ctx` is the same assembly
`Context` object as today).

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

Tool bodies keep receiving whatever arguments they receive today (their own
declared parameters, plus `self`). No `AppContext`/`TurnContext` argument
is introduced in this part.

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

Hook bodies keep the same signature shape they have today for their type —
a `TRANSFORM_TURN` hook still takes `(entry, age, ctx)`, a `STARTUP` hook
still takes whatever `register_runtime` handlers take today (the `runtime`
object), and so on. This part changes *how the method gets registered*,
not what it's called with.

### `@command`

```python
def command(namespace: str, sub: str = "", *, permissions,
            help: str = "", params: ParamSpec | None = None): ...
```

Wraps `CommandRegistry.register` with its existing semantics intact.
Commands are **not** tools and keep their own shape:

- Handler signature is `(args: list[str], context)` — a token list and the
  same raw dispatch dict channels assemble today — not typed keyword
  arguments. It returns the string to send, or `None`.
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

Two changes to the handler contract (independent of the facade question,
kept in this part because they are call-signature fixes, not access-scope
changes):

**Handlers return their output instead of calling `send`.** Every command
handler in the codebase today ends in `await send(...)` followed by
`return` — `_cmd_info`, `_cmd_rename` and `_cmd_modify_permissions` in
`runtime.py` do it in every branch, and none calls `send` twice or does
work after it. That is a return value written as a side effect. Returning
shortens each early exit from two lines to one, makes a handler testable
without a mock `send`, and matches `@tool`, which already returns.

It also closes a real failure mode: `dispatch()` wraps handlers in
try/except and logs, so a handler that raises *after* calling `send` has
already emitted output while one that raises before emits nothing, and the
caller cannot tell which. With a return, "produced output" and "completed
successfully" become the same event.

`context["reply"]` (or equivalent) stays for the genuine streaming case — a
long-running command that emits progress before it finishes. `return None`
means handled with nothing to say. The permission-denial path in
`dispatch()` resolves `send` itself and is unaffected.

**The context argument stays a raw dict in this part** — `context["send"]`,
`context["runtime"]`, `context["console"]`, exactly as today. Wrapping it in
a `CommandContext` object is Part 2's job (it's the same facade decision as
`AppContext`/`TurnContext`); doing it here would mix a registration-shape
fix with an access-scope fix in one commit.

### Tool return values

A tool returns whatever it wants the model to read, normally a plain
string. There is no result envelope and no `self.ok()` / `self.error()`
helper.

An envelope was considered and rejected. `agent.py::_execute_tool` already
wraps every return into a `ToolResult`, so a second `{"status": ...,
"content": ...}` layer inside it is JSON noise around what is often three
words, and it adds framework vocabulary an author must learn to write the
most common line in any module.

Expected failures — the ones the model should read and adapt to, not
crashes — raise instead:

```python
raise ToolError("note already exists; read it and edit instead")
```

`_execute_tool` catches `ToolError` and renders it consistently, so error
formatting is the framework's job rather than each author's. Unexpected
exceptions keep their existing handling. The permission layer already
produces its own `[PERMISSION DENIED] ...` strings and is unaffected.

### Tool timeouts

`@tool(timeout=...)` bounds one call, and the default is **deliberately
generous: 600s**. Nothing bounds tool execution today, so a wedged
`open_url` or MCP call blocks the cycle forever.

The default is high because the two failure modes are not symmetric. A
timeout that never fires leaves a visible hang — the turn stalls and the
operator notices. A timeout that fires early presents as a bug in the tool
itself, and costs real debugging time: `comfyui` once shipped a 10s
timeout, far below what image generation takes, so every call failed and
the symptom read as "ComfyUI is broken" rather than "TinyCTX gave up
early". A too-low timeout is indistinguishable from a broken integration
from the outside. The framework timeout exists to stop a hung call blocking
the cycle indefinitely, not to enforce responsiveness.

Per-tool overrides raise or lower it; `modules/shell` already implements
this shape correctly (`default_timeout: 120`, `max_timeout: 1200`, per-call
override clamped to the max) and keeps its own values.

Module-level timeouts are separate and unaffected. A module's own timeout
must be set to what its work actually takes, and the framework default must
sit above the slowest tool it wraps, or the outer bound silently truncates
the inner one.

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

### Lifecycle

There are no mandatory lifecycle methods. Startup, shutdown and background
work are hook types like any other:

- `@hook(HookType.STARTUP)` — replaces `register_runtime`. Body still
  receives `runtime` (the raw object), same as `register_runtime(runtime)`
  does today.
- `@hook(HookType.SHUTDOWN)` — cleanup
- `@hook(HookType.BACKGROUND)` — the loader starts it as an `asyncio.Task`
  and cancels it on shutdown. `cron`, `heartbeat` and memory's librarian
  loops each hand-roll `create_task` plus their own cancellation today.
- `@hook(HookType.TURN_START)` — per-turn wiring: enabling a tool for this
  turn, registering a prompt provider bound to this turn's data. Body still
  receives `cycle` (the raw `AgentCycle`), same as `register_agent(cycle)`
  does today.

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

`Scratch` is the *only* new object this part introduces into a hook's call
signature. Everything else the hook already received (`entry`, `age`,
`ctx`, `runtime`, `cycle`) it keeps receiving, unfacaded.

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
the mechanism channels use (channel-specific tool exposure is finished in
Part 3, but the `platforms` field itself is introduced here since it's a
`Module` class attribute, not a facade concern).

**No `AppContext`, no `TurnContext`.** A module instance in this part talks
to the framework by receiving `runtime`/`cycle`/`context`/`agent` as
arguments to its tagged methods, exactly as `register_runtime(runtime)` and
`register_agent(cycle)` hand them over today. `self.manager`-style instance
attributes are not introduced either — if a module needs `runtime` outside
its `STARTUP` hook, it stashes the reference itself (`self.runtime = runtime`
in `open_store`, as in the example above), same as today's modules do.

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
Moving to process-lifetime registration is P2, once modules are classes
with tagged methods to register from.

Verify: 886 green; `test_hook_order.py` byte-identical; new tests that a
non-`HookType` raises and that `STREAM_TEXT` takes the no-wrapper path.

### P2 — Decorators and Module class, proven on three
1. Add `TinyCTX/module.py` — `Module`, `Scratch` namespacing, settings-
   schema merge. **No `AppContext`/`TurnContext`/`CommandContext` — hook
   and tool bodies keep taking `runtime`/`cycle`/`context`/raw `dict`.**
2. Add `TinyCTX/decorators.py` — `@tool`, `@hook`, `@command`, plus
   `ToolError` and the 600s default tool timeout.
3. The loader gains the class path alongside the function path. Temporary
   scaffolding for P2 only, not a compatibility interface.
4. Migrate three modules chosen to stress different corners: `todo`
   (simplest, proves the shape), `ctx_tools` (heaviest hook user, proves
   scratch replaces closure state and that same-type-different-priority
   tagging works — see the worked comparison below), `shell` (proves
   `@tool` carries a callable classifier and `listing_permissions` intact).

`shell` is the one that decides whether `@tool` is sufficient. If its
`required_permissions_for_shell` classifier survives decoration unchanged,
every other tool will.

Verify: 886 green; `test_hook_order.py` identical.

#### Worked comparison: `ctx_tools` dedup, before and after

Before (today — closures faking per-pass state):

```python
def _register_dedup(context, config):
    dedup_after = config.get("same_call_dedup_after", 3)
    suppressed_tool:  set[int] = set()
    suppressed_calls: set[str] = set()

    def pre_assemble(ctx):
        suppressed_tool.clear()
        suppressed_calls.clear()
        ...

    def filter_turn(entry, age, ctx):
        if entry.role == "tool" and entry.index in suppressed_tool:
            return False

    context.register_hook("pre_assemble",   pre_assemble,   priority=0)
    context.register_hook("filter_turn",    filter_turn,    priority=0)
```

After (this part — decorated methods, scratch instead of closures, `ctx`
still the raw assembly `Context`):

```python
class CtxTools(Module):
    settings = {"same_call_dedup_after": {"default": 3, "type": "int", ...}}

    @hook(HookType.PRE_ASSEMBLE, priority=0)
    async def dedup_scan(self, ctx, scratch):
        dedup_after = self.config["same_call_dedup_after"]
        scratch.suppressed_tool = set()
        scratch.suppressed_calls = set()
        ...

    @hook(HookType.FILTER_TURN, priority=0)
    async def dedup_filter(self, entry, age, ctx, scratch):
        if entry.role == "tool" and entry.index in scratch.suppressed_tool:
            return False
```

What changed: the two module-level closures and the `config.get(...)`
boilerplate are gone, replaced by `self.config` and `scratch`. What did
*not* change: `ctx` is still the same assembly-pass object the hook body
reasons about; no facade object was introduced.

### P3 — Hard migrate
1. Port the remaining ~17 modules plus `custom_modules/ip_rewrite`.
2. All hook registration moves to decoration. The registry becomes
   process-lifetime.
3. Migrate the 6 `commands.register()` call sites (runtime.py:257/263/267,
   memory:435/448, sysops:174) to `@command`; convert each handler's
   `await send(x); return` to `return x`. The `context` argument stays the
   raw dict in this part (see `@command` above) — `CommandContext` is
   Part 2.
4. Add `ToolError` and the `_execute_tool` catch. Convert tools that
   currently signal expected failure with an ad-hoc `"Error: ..."` string
   prefix to raise it.
5. Delete `register_runtime`/`register_agent` from the loader, and
   `EXTENSION_META` throughout.
6. Delete `outbound_events`, `_memory_block`, `_file_read_state` — **not
   in this part.** These are reach-through problems Part 2's facade fixes
   by giving them a proper scoped home (`turn.scratch`, `turn.emit`). Under
   this part alone they'd have nowhere else to go, so they stay exactly as
   they are, still reached into directly. Flagging here so P3 does not
   accidentally delete them without Part 2 having landed a replacement.
7. Rewrite `for-contributors/module_template/` as one annotated file
   reflecting Part 1's shape (decorators + raw framework objects, no
   facade).

Verify: 886 green; `grep -rn "register_agent\|register_runtime\|EXTENSION_META" TinyCTX/`
returns nothing outside docs. Add a test asserting the handler count for a
given type is stable across 50 consecutive turns — the direct regression
test for re-registration creeping back.

**Before starting P3, inventory `custom_modules/`.** It is gitignored, so
only `ip_rewrite` is visible from the repo. Anything else living there is
invisible to this plan and breaks at step 4 with no warning.

---

## Risks

**Ordering changes are silent.** The only defence is `test_hook_order.py`
existing before P1 starts. Nothing else in the suite catches a reordering
inside `TRANSFORM_TURN`.

**`STREAM_TEXT` is a per-token path.** If `emit` wraps it in the generic
isolated path, throughput drops on every response. `isolate=False` needs a
fast path with no wrapper allocation and its own test. **Add a throughput
benchmark to P1's verify list, not just a correctness test** — this is the
one place a naive generic implementation regresses perf silently.

**Decorators run at class-definition time**, before any instance exists.
They can only record metadata onto the function object; actual registration
happens when the loader walks the instantiated class. A decorator that
tries to touch `self`, config, or a framework object will fail at import.

**Callable permission classifiers must keep receiving tool arguments by
name.** `required_permissions_for_shell(command, timeout, backend_access, **_ignored)`
is called as `required_fn(**args)`. If `@tool` ever reshapes a tool's
signature, `shell` and `present` break in a way that fails *open* on a
`TypeError` path unless the handler's existing catch keeps worst-casing.
Verify that path explicitly in P2.

**Command handlers are not tools** and must not be unified with them.
Different signature (`(args, context)` vs typed kwargs), different
registry, separate `params` for native slash commands, dispatch by
two-word grammar. `@command` wraps the existing registry; it does not
merge it.

**Scratch namespacing is a correctness boundary.** `ctx_tools` runs three
handler groups with separate working sets. Per-module namespacing with
distinct keys is fine; flattening them is not.

**Leaving reach-through in place is a deliberate, temporary choice.** This
part does not fix defect #4 from the original plan (modules reaching
`agent.config` 40x, `agent.context` 40x, `cycle._memory_block`,
`agent._file_read_state`) or the bridges-can't-extend-the-agent gap.
Anyone reading only this document might reasonably ask why those aren't
fixed alongside the registration rework — the answer is that fixing them
requires the facade decision (Part 2), which is a separate, larger, and
more debatable call than "make registration explicit and typed." Shipping
Part 1 alone is still a net improvement and does not foreclose Part 2.

**P3 is one large diff by construction.** That is the cost of the hard
migrate. P2 (of this part) is what makes it safe: by the time P3 starts the
shape has been proven against the two hardest modules.

---

## Deliberate deferrals (to Part 2, Part 3, or indefinitely)

- **`AppContext`, `TurnContext`, `CommandContext`.** The whole facade
  question — see `MODULES-PLAN-P2.md`.
- **Channels.** `bridges/` → `channels/`, `Channel(Module)`,
  platform-scoped tool exposure — see `MODULES-PLAN-P3.md`.
- **Module-defined hook types.** The enum is closed. An open
  `register_type` would let a typo'd custom type silently create a second
  bucket — the defect being fixed.
- **Per-type hook decorator aliases** (`@transform_turn(priority=10)`).
  Nicer at the call site, but a second set of names parallel to `HookType`
  that can drift. If wanted later, generate them from the enum so they
  cannot.
- **Background/long-running tools.** A tool that returns a job id
  immediately and delivers its result later touches `Runtime`, `Run`,
  `Exogenous` and a new job registry — not the module system. `@tool` would
  gain a flag, but the machinery belongs in its own plan.
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

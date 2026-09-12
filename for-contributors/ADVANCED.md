# Advanced module patterns

Not part of the beginner template (`module_template/__init__.py`). These are
real patterns used elsewhere in the codebase, but each one is a deliberate
escape hatch, not something to reach for by default.

## A tool that needs live per-turn state

`@tool` methods receive only their own declared, model-visible arguments —
never `ctx`, `cycle`, or `runtime`. This isn't a missing feature: the loader
builds the tool's JSON schema straight from its Python signature
(`inspect.signature`), so any parameter you add is something the model has to
invent a value for. There's no way to add a `cycle` parameter without asking
the model to fill it in, and no reason to want that — it would mean model
output flowing directly into a live `AgentCycle` or the database, which is
exactly the kind of path that can corrupt state if the model hands back
something malformed or stale.

If a tool genuinely needs something that changes every turn — who's calling
(`cycle.caller`), enabling another tool mid-turn (`cycle.tool_handler.enable()`),
a live browser/session object — register it *imperatively* from a
`@hook(HookType.TURN_START)` method instead. TURN_START does receive `cycle`,
once per `AgentCycle`, and can close over it in a nested function:

```python
@hook(HookType.TURN_START)
def wire_live_tool(self, cycle) -> None:
    def needs_live_cycle(arg: str) -> str:
        """Uses cycle state that changes every turn.

        Args:
            arg: Example argument.
        """
        return f"caller was {cycle.caller.username}: {arg}"

    cycle.tool_handler.register_tool(
        needs_live_cycle, always_on=False, required_permissions=None,
    )
```

This is the established way to do it, not a workaround — `modules/present`,
`modules/sysops`, `modules/cron`, and `modules/filesystem` all do this. See
`modules/present/__init__.py`'s own module docstring for more detail.

## Writing session state from inside a @tool

A `@tool` has no `ctx`, so it can't call `ctx.db.get_state`/`set_state`
directly. If a tool genuinely needs to persist state, wire it from
`TURN_START` the same way as above, and use `cycle.db` /
`cycle.context.tail_node_id` in place of `ctx.db` / `ctx.tail_node_id`:

```python
@hook(HookType.TURN_START)
def wire_state_tool(self, cycle) -> None:
    def set_example_state(value: str) -> str:
        """Persist a value in session state so future turns on this branch can read it.

        Args:
            value: The value to remember.
        """
        cycle.db.set_state(cycle.context.tail_node_id, STATE_KEY, value)
        return "saved"

    cycle.tool_handler.register_tool(
        set_example_state, always_on=False, required_permissions=None,
    )
```

## The two things "state" can mean

- **Session state** — a plain dict reconstructed by walking a conversation
  branch's ancestor chain, merging each node's `state_delta` JSON column
  (most-recent node wins per key). This is what `ctx.db.get_state`/`set_state`
  (or `cycle.db...` from TURN_START) read and write, and what survives across
  turns on the same branch.
- **`ctx.state`** — a plain dict attribute on the live `Context` object,
  scoped to a single `assemble()` call (a fresh dict every time it runs — not
  the same mechanism as a hook's `scratch` parameter, which is dropped after
  each *individual* assemble() call, whereas `ctx.state` persists across every
  assemble() call within one `AgentCycle`). After `assemble()` runs,
  `ctx.state["session"]` holds the *same* session-state dict described above
  (loaded internally via `db.load_session_state()`), plus bookkeeping keys
  like `ctx.state["tokens_used"]`. A hook or prompt provider that only needs
  to *read* session state should use `ctx.state["session"]` rather than
  calling `load_session_state()` itself.

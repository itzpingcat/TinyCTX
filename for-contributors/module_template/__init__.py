"""
__init__.py — TinyCTX module template (MODULES-PLAN-P1.md shape).

HOW TO USE THIS FILE
---------------------
Copy this whole folder to TinyCTX/modules/<your_module_name>/ (or
TinyCTX/custom_modules/<your_module_name>/ if it's a user-local plugin that
shouldn't be committed to the repo — same interface, just gitignored).
Rename the class and settings keys, trim out whichever sections you don't
need, delete the example tools/hooks, keep the comments you still find
useful.

MODULE DISCOVERY
-----------------
TinyCTX/module_registry.py scans TinyCTX/modules/ and TinyCTX/custom_modules/
at startup. Any subdirectory containing __main__.py or __init__.py is a
candidate, and it's loaded if it defines exactly one class inheriting from
TinyCTX.module.Module. The loader instantiates it once (process lifetime)
and walks its @tool/@hook/@command/@prompt-decorated methods to register
them — you never call a registration function yourself.

WHAT A MODULE BODY RECEIVES
-----------------------------
There is no AppContext/TurnContext facade. Method bodies take the same raw
framework objects modules always have:
  - `runtime`  — the shared TinyCTX.runtime.Runtime (STARTUP only)
  - `cycle`    — the live TinyCTX.agent.AgentCycle (per-turn hooks only)
  - `ctx`      — the assembly-pass Context (most hooks/prompts)
  - a plain `dict` (command handlers' `context` argument)
`@tool` bodies are the one exception: they receive ONLY their own declared,
model-visible arguments — no `ctx`/`cycle` at all (see the TURN_START
section below for what to do when a tool genuinely needs live per-cycle
state).
"""
from __future__ import annotations

import logging

from TinyCTX.decorators import command, hook, prompt, tool
from TinyCTX.hooks import HookType
from TinyCTX.module import Module, ToolError
from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)

# Give your module's session-state key a unique, namespaced name so it can't
# collide with another module's key or with the built-in keys written by
# Runtime._compute_state_delta() (those are: platform, author_id, agent_name,
# server_name, channel_name — never write to those from a module).
STATE_KEY = "example_module_value"


class ExampleModule(Module):
    """One or two sentences describing what this module does and which
    tools/prompts it adds. This shows up in tooling that lists modules —
    keep it accurate and current as the module evolves."""

    # ------------------------------------------------------------------
    # SETTINGS — a declarative schema, not a flat defaults dict. Merged
    # once by the loader with config.yaml's `extra.<module_name>.<key>`
    # and exposed as self.config (a plain dict of resolved values).
    #
    # Deployers override any of these per-instance via config.yaml:
    #
    #     extra:
    #       example_module:
    #         prompt_priority: 12
    #
    # Never require a config.yaml edit just to run with sane defaults —
    # every tunable your module reads should have a default here.
    # ------------------------------------------------------------------
    settings = {
        "prompt_priority": {
            "default": 8,
            "type": "int",
            "description": "System prompt priority for this module's standing context block.",
        },
        # "some_other_setting": {"default": "value", "type": "str", "description": "..."},
    }

    # NOTE: if one of your settings is itself a nested dict (e.g. a
    # {enabled, threshold} group) and callers should be able to override
    # just one subkey without wiping the rest, override resolve_settings()
    # to deep-merge instead of the base's whole-value replace — see
    # modules/system_prompt or modules/memory for the exact pattern.

    def __init__(self) -> None:
        # Instance attributes are process-lifetime (one instance for the
        # whole process, shared across every AgentCycle) — the right home
        # for a cache, a DB connection, a background task handle, anything
        # that shouldn't be rebuilt every turn.
        self._some_process_lifetime_state: str | None = None

    # ======================================================================
    # STARTUP — replaces the old register_runtime(runtime). Runs once, at
    # process start, before any AgentCycle exists.
    # ======================================================================

    @hook(HookType.STARTUP)
    def load(self, runtime) -> None:
        """
        Use this for:
          - building singletons shared across all cycles/users (e.g. a DB
            connection, an index, a scheduler — see modules/memory, modules/cron)
          - kicking off background asyncio tasks
          - anything that only depends on config, not on a live turn

        `runtime` owns runtime.db (ConversationDB), runtime.users (UserStore),
        runtime.commands (CommandRegistry), runtime.module_registry.

        self.config is already populated (from `settings` above, merged with
        config.yaml) by the time this runs.

        Delete this method entirely if your module has no startup-only work.
        """
        logger.info("[example_module] loaded")
        self._some_process_lifetime_state = "ready"

    # ======================================================================
    # TOOLS — @tool-decorated methods. The loader introspects the method
    # signature and docstring to build the JSON schema the LLM sees — you
    # never hand-write a schema.
    #
    # Docstring convention (this is what gets parsed):
    #   - First paragraph  -> tool description shown to the LLM
    #   - "Args:" block     -> per-parameter descriptions
    #
    # Type annotations map to JSON schema types. Stick to plain str, int,
    # bool, list, dict. Bare `list` always becomes {"type": "array", "items":
    # {"type": "string"}}. Parametrized generics like `list[str]` are NOT
    # reliably parsed under `from __future__ import annotations` (they fall
    # back to {"type": "string"}, which is wrong but won't crash) — see
    # CODEBASE.md's "Tool System" section. When in doubt, use bare `list`.
    #
    # Parameters without a default value are required; parameters with a
    # default are optional.
    #
    # `permissions` is a REQUIRED keyword — omitting it is a TypeError at
    # class-definition time. Pass a set[Permission] (static), None
    # (deliberately ungated), or a callable(**call_args) -> set[Permission]
    # for per-argument classification (see modules/shell/perms.py,
    # modules/present's _present_perms for real examples).
    # ======================================================================

    @tool(
        # always_on=True sends this tool's schema to the LLM on EVERY turn —
        # expensive in tokens if you have many tools. Prefer always_on=False
        # and let the always-on `tools_search` tool (BM25 over tool name +
        # description) enable it on demand. Only set True for tools the
        # agent truly needs available at all times.
        always_on=False,
        permissions=None,  # or e.g. {Permission.FILE_WRITE}, or a classifier callable
    )
    def example_tool(self, text: str, shout: bool = False) -> str:
        """One-line summary shown to the LLM as the tool description.

        Args:
            text: The text to transform.
            shout: If true, uppercase the result.
        """
        if not text:
            # Expected failure — the model should read this and adapt, not a
            # crash. Raise ToolError rather than `return "Error: ..."`; the
            # framework renders it consistently instead of each tool author
            # inventing its own prefix.
            raise ToolError("text must not be empty")
        return text.upper() if shout else text

    # async def is also supported and is awaited directly — use it for
    # I/O-bound work (HTTP calls, subprocess) so it doesn't block the event
    # loop. Sync methods instead run in a thread-pool executor. Both are
    # dispatched identically by ToolCallHandler.execute_tool_call.
    #
    # @tool(always_on=False, permissions={Permission.NETWORK_READ})
    # async def example_async_tool(self, url: str) -> str:
    #     """Fetch a URL and return its text.
    #
    #     Args:
    #         url: The URL to fetch.
    #     """
    #     ...

    # ======================================================================
    # PROMPT PROVIDERS — @prompt-decorated methods. Each returns a string
    # (or None/"" to contribute nothing this turn) injected into the system
    # prompt (or another role) EVERY turn. All system-role providers are
    # concatenated into a single system message during Context.assemble().
    # Keep these cheap since they run on every single turn.
    #
    # `priority`, like every decorator argument, is fixed at class-definition
    # time — it can't read self.config, since no instance exists yet when
    # the decorator runs. Hardcode it to your settings schema's default (see
    # modules/equipment_manifest's docstring for the full reasoning).
    # ======================================================================

    @prompt(role="system", priority=8, name="example_module")
    def status_prompt(self, ctx) -> str:
        # ctx.state["session"] holds the session-state dict for THIS
        # assemble pass (see the SESSION STATE section below) — reading it
        # here does not require a DB call of your own.
        session = ctx.state.get("session", {})
        value = session.get(STATE_KEY)
        if not value:
            return ""
        return f"<example_module_state>{value}</example_module_state>"

    # ======================================================================
    # CONTEXT HOOKS — @hook-decorated methods. Context.assemble() runs a
    # pipeline of hooks each turn, in this order:
    #
    #   1. PRE_ASSEMBLE_ASYNC   async def fn(self, ctx) -> None
    #        Awaited by AgentCycle BEFORE assemble() is called (assemble()
    #        itself is synchronous, so an async hook body only belongs
    #        here). Use this for anything requiring `await` — DB reads,
    #        network/embedding calls. See modules/rag's auto-inject prefetch
    #        or modules/memory's memory_block refresh for real examples,
    #        including how to bound a slow prefetch with a timeout instead
    #        of racing assemble() with a detached task.
    #
    #   2. (DB history for the branch is loaded internally)
    #
    #   3. PRE_ASSEMBLE         def fn(self, ctx, scratch=None) -> None
    #        Sync, runs at the very start of assemble() (e.g. warm a cache,
    #        scan the dialogue once for later stages to reuse).
    #
    #   4. FILTER_TURN          def fn(self, entry, age, ctx, scratch=None) -> bool
    #        Return False to drop a history turn entirely from the
    #        assembled message list.
    #
    #   5. TRANSFORM_TURN       def fn(self, entry, age, ctx, scratch=None) -> HistoryEntry | None
    #        Replace or compress a turn (e.g. summarize an old tool result
    #        to save tokens). Return None to leave it unchanged. Multiple
    #        methods may tag TRANSFORM_TURN at different `priority` values —
    #        see modules/ctx_tools for a module with three.
    #
    #   (adjacent same-role messages are merged automatically)
    #
    #   6. POST_ASSEMBLE        def fn(self, messages, ctx, scratch=None) -> list[dict] | None
    #        Final reshape of the fully assembled OpenAI-format message
    #        list. Return None to leave it unchanged.
    #
    #   (token budget trimming happens last, dropping oldest non-system turns)
    #
    # `scratch` — add a trailing `scratch` parameter to any of the four sync
    # stages above (its presence is detected by signature, so it's entirely
    # optional) to share derived data between your own hook methods within
    # ONE assemble() pass, instead of faking it with closure state (hooks
    # are registered once per process, so they cannot close over per-turn
    # data the way the old function-based modules did). Pick a namespaced
    # attribute name so it can't collide with another module's — see
    # modules/ctx_tools's dedup/trim hooks for the pattern.
    #
    # Example (commented out — uncomment and adapt if your module needs one):
    #
    # @hook(HookType.FILTER_TURN, priority=10)
    # def drop_old_debug_turns(self, entry, age, ctx) -> bool:
    #     return not (age > 50 and "debug" in (entry.content or ""))
    # ======================================================================

    # ======================================================================
    # TOOLS/HOOKS THAT NEED LIVE PER-CYCLE STATE
    # ======================================================================
    # @tool methods receive only their own declared arguments — no `cycle`,
    # no `ctx`. If a tool genuinely needs something that changes per turn
    # (cycle.caller, cycle.outbound_events, a live browser session, tool-
    # enablement via cycle.tool_handler.enable(), ...), register it
    # imperatively from a @hook(HookType.TURN_START) method instead — that
    # DOES receive `cycle`, once per AgentCycle, and can close over it in a
    # nested function the way the old register_agent(cycle) used to. This is
    # the common case, not a workaround: modules/present, modules/sysops,
    # modules/cron, and modules/filesystem all do this. See
    # modules/present/__init__.py's own module docstring for the full
    # rationale (a facade fixing this is deferred to a later plan phase).
    #
    # @hook(HookType.TURN_START)
    # def wire_live_tool(self, cycle) -> None:
    #     def needs_live_cycle(arg: str) -> str:
    #         """Uses cycle state that changes every turn.
    #
    #         Args:
    #             arg: Example argument.
    #         """
    #         return f"caller was {cycle.caller.username}: {arg}"
    #
    #     cycle.tool_handler.register_tool(
    #         needs_live_cycle, always_on=False, required_permissions=None,
    #     )

    # ======================================================================
    # SLASH COMMANDS — @command-decorated methods. Only appropriate when the
    # handler needs nothing beyond self.* (process-lifetime state) plus the
    # per-call `context` dict CommandRegistry.dispatch() already passes
    # every handler — see modules/sysops's /model for a real example. If
    # your command needs live cycle state, there is no live cycle at
    # dispatch time (commands run outside an AgentCycle) — resolve whatever
    # you need from `context` itself (context["runtime"], a resolved
    # caller, etc.), the same way /model does.
    #
    # Handlers RETURN the string to send (or None for nothing to say) —
    # dispatch() delivers it via context["send"]/["console"] itself. Don't
    # call context["send"] yourself unless you're genuinely streaming
    # progress before the call finishes.
    # ======================================================================

    @command("example", "", permissions=None,
             help="One-line help text shown by /help.")
    async def cmd_example(self, args: list[str], context: dict) -> str | None:
        return f"example command called with args={args}"

    # ======================================================================
    # SESSION STATE ("the State system")
    # ======================================================================
    # TinyCTX has no dedicated State class. "State" refers to two related
    # but distinct things:
    #
    # (a) SESSION STATE — a plain dict reconstructed by walking a
    #     conversation branch's ancestor chain in ConversationDB, merging
    #     each node's `state_delta` JSON column (most-recent node wins per
    #     key). This is how a value survives across multiple turns on the
    #     same branch without a dedicated database table. Real examples:
    #     modules/rag stores "rag_auto_targets" this way; modules/skills
    #     stores "skills_expanded_categories" this way. Both write it from a
    #     tool call and read it back in a prompt provider or hook on a later
    #     turn.
    #
    #     Use db.get_state / db.set_state — NOT the raw
    #     load_session_state / update_node_state_delta primitives — for
    #     anything module-level. Here's why: update_node_state_delta()
    #     blindly REPLACES a node's entire state_delta column. If your tool
    #     call does
    #         db.update_node_state_delta(node_id, json.dumps({STATE_KEY: value}))
    #     and some other module (or another tool call earlier in the same
    #     turn) already wrote a different key onto that exact node_id, your
    #     write silently erases it. set_state() avoids this by reading the
    #     node's existing delta first and merging your key in before writing
    #     it back — safe no matter who else touches that node. get_state()
    #     is the matching single-key read (a thin wrapper over
    #     load_session_state(), which walks the whole ancestor chain — only
    #     the WRITE side needed fixing).
    #
    #     `ctx.db` is a public property (added alongside the Module system)
    #     so a hook/prompt tagged against `ctx` alone can reach it without
    #     needing live `cycle`:
    #         value = ctx.db.get_state(ctx.tail_node_id, STATE_KEY, default=None)
    #         ctx.db.set_state(ctx.tail_node_id, STATE_KEY, value)
    #     From inside a @tool that needs live cycle state (see the TURN_START
    #     section above), use cycle.db / cycle.context.tail_node_id instead.
    #
    # (b) ctx.state — a plain dict attribute on the live Context object,
    #     scoped to a single assemble() call (a FRESH dict, dropped, every
    #     time it runs — this is `scratch`'s older, coarser-grained cousin,
    #     not the same mechanism: ctx.state persists across every assemble()
    #     call within one AgentCycle, where Scratch is dropped after each
    #     individual assemble() call — see modules/output_parser's
    #     nudge-budget state for why that distinction matters). After
    #     assemble() runs, ctx.state["session"] holds the SAME session-state
    #     dict described in (a) — Context loads it internally via
    #     db.load_session_state() for convenience — plus bookkeeping keys
    #     like ctx.state["tokens_used"]. Hooks and prompt providers that only
    #     need to READ session state (not write it) should use
    #     ctx.state["session"] rather than calling load_session_state()
    #     themselves — see status_prompt() above.

    @tool(always_on=False, permissions=None)
    def set_example_state(self, value: str) -> str:
        """Persist a value in session state so future turns on this branch can read it.

        This is a @tool, so it has no `ctx` — but set_example_state has no
        need for live cycle state either (it only needs a node id and a DB
        handle, both of which... wait, a plain @tool genuinely can't reach
        ctx.tail_node_id at all. If you need this exact shape for real, wire
        it from TURN_START instead, closing over `cycle`, the same as any
        other tool needing live per-cycle state — see that section above.
        This method is left here unregistered-in-practice, as a worked
        example of the state read/write calls themselves, not a template to
        copy verbatim.

        Args:
            value: The value to remember.
        """
        raise ToolError("see this method's docstring — wire from TURN_START instead")


# ======================================================================
# CHECKLIST FOR A NEW MODULE
# ======================================================================
# 1. Copy this folder to TinyCTX/modules/<name>/ (or custom_modules/<name>/
#    for a gitignored, user-local plugin).
# 2. Rename the class, and update `settings` with every tunable your module
#    reads, each with a `default`, `type`, and `description`.
# 3. Delete whichever example sections you don't need (STARTUP, tools,
#    prompts, hooks, commands).
# 4. Register tools via @tool. Default to always_on=False; use
#    required `permissions=` — a set[Permission], None, or a classifier
#    callable — never omit it (that's a startup assertion failure, not a
#    silently-ungated tool).
# 5. If a tool/hook genuinely needs live per-turn state (cycle.caller,
#    cycle.outbound_events, tool_handler.enable(), ...), wire it
#    imperatively from @hook(HookType.TURN_START) — see that section above.
# 6. Register prompt providers via @prompt if the agent needs standing
#    context injected every turn.
# 7. Register context hooks via @hook(HookType.<STAGE>) only if you need to
#    filter/transform/reshape history or prompt assembly. Use `scratch` to
#    share data between your own hook methods within one assemble() pass.
# 8. For cross-turn memory, use ctx.db.get_state / ctx.db.set_state (or
#    cycle.db.../cycle.context.tail_node_id from a TURN_START-wired tool)
#    with your own unique STATE_KEY — never write to the built-in keys
#    (platform, author_id, agent_name, server_name, channel_name). Don't use
#    db.update_node_state_delta / db.load_session_state directly from module
#    code — set_state/get_state wrap them safely (see the SESSION STATE
#    section above for why the raw write primitive is a footgun).
# 9. Raise TinyCTX.module.ToolError for expected failures instead of
#    returning an ad-hoc "Error: ..." string — the framework renders it
#    consistently.
# 10. Run linters, and if this is a new top-level module, add it to the
#     project layout list in TinyCTX/CODEBASE.md.

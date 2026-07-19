"""
__main__.py — TinyCTX module template.

HOW TO USE THIS FILE
---------------------
Copy this whole folder to TinyCTX/modules/<your_module_name>/ (or
TinyCTX/custom_modules/<your_module_name>/ if it's a user-local plugin that
shouldn't be committed to the repo — same interface, just gitignored).
Rename this file's containing folder, then trim out whichever sections below
you don't need. Delete the example tools/hooks, keep the parts of the
comments you still find useful.

MODULE DISCOVERY
-----------------
TinyCTX/module_registry.py scans TinyCTX/modules/ and TinyCTX/custom_modules/
at startup. Any subdirectory containing __main__.py or __init__.py is a
candidate module, and it's loaded if it defines at least one of:

    register_runtime(runtime)  -> None   # called ONCE at process startup
    register_agent(cycle)      -> None   # called once per AgentCycle (i.e. per turn)

Both are optional — a module can implement just one of them. A module that
only needs startup work (e.g. registering a slash command, starting a
background loop) skips register_agent. A module that only needs per-turn
wiring (tools, prompts, hooks) skips register_runtime.

CONFIG: this folder also has an __init__.py declaring EXTENSION_META with a
"default_config" dict. That's not required for discovery — it's a
convention (see modules/todo, modules/rag, modules/skills, modules/cron)
for declaring your module's tunables and their defaults in one place, so
deployers can override them per-instance from config.yaml without editing
code. See the "Config" section inside register_agent below for how the
merge works.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Give your module's session-state key a unique, namespaced name so it can't
# collide with another module's key or with the built-in keys written by
# Runtime._compute_state_delta() (those are: platform, author_id, agent_name,
# server_name, channel_name — never write to those from a module).
STATE_KEY = "example_module_value"


# ======================================================================
# register_runtime — called once at startup
# ======================================================================
def register_runtime(runtime) -> None:
    """
    Runs once per process, before any AgentCycle exists. Use this for:
      - building singletons shared across all cycles/users (e.g. a DB
        connection, an index, a scheduler)
      - registering slash commands on runtime.commands
      - kicking off background asyncio tasks (e.g. modules/cron does this)

    `runtime` is the shared TinyCTX.runtime.Runtime instance — it owns
    `runtime.db` (ConversationDB), `runtime.users` (UserStore),
    `runtime.commands` (CommandRegistry), `runtime.module_registry`.

    Delete this function entirely if your module has no startup-only work —
    module_registry.py checks with hasattr() and skips it if absent.
    """
    logger.info("[example_module] register_runtime called")
    # Example: runtime.commands.register("/example", some_handler_fn)


# ======================================================================
# register_agent — called once per AgentCycle (i.e. once per turn)
# ======================================================================
def register_agent(cycle) -> None:
    """
    Runs once per AgentCycle, after cycle.tool_handler and cycle.context
    already exist. This is where you register tools, prompt providers, and
    context hooks. Everything below (tools section, prompts section, hooks
    section, state section) happens inside this one function — split it up
    however you like once your module grows.

    Delete this function entirely if your module only does startup work.
    """

    # ------------------------------------------------------------------
    # CONFIG — default_config (from __init__.py's EXTENSION_META) merged
    # with any per-instance override from config.yaml's `extra:` block.
    # ------------------------------------------------------------------
    # This is the exact pattern modules/todo/__main__.py uses. Skip this
    # block entirely if your module has no tunables.
    try:
        from TinyCTX.modules.example_module import EXTENSION_META  # rename to your module's package path
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}
    if hasattr(cycle.config, "extra") and isinstance(cycle.config.extra, dict):
        # config.yaml:
        #   extra:
        #     example_module:
        #       prompt_priority: 12
        runtime_cfg = cycle.config.extra.get("example_module", {})  # match EXTENSION_META["name"]
        cfg = {**cfg, **runtime_cfg}  # runtime config wins over the module's own default

    # ------------------------------------------------------------------
    # TOOLS
    # ------------------------------------------------------------------
    # Tools are plain Python functions. cycle.tool_handler.register_tool()
    # introspects the function signature and docstring to build the JSON
    # schema the LLM sees — you never hand-write a schema.
    #
    # Docstring convention (this is what gets parsed):
    #   - First paragraph  -> tool description shown to the LLM
    #   - "Args:" block     -> per-parameter descriptions
    #
    # Type annotations map to JSON schema types. Stick to plain str, int,
    # bool, list, dict. Bare `list` always becomes {"type": "array",
    # "items": {"type": "string"}}. Parametrized generics like `list[str]`
    # are NOT reliably parsed under `from __future__ import annotations`
    # (they fall back to {"type": "string"}, which is wrong but won't
    # crash) — see CODEBASE.md's "Tool System" section for the exact
    # caveat. When in doubt, use bare `list`.
    #
    # Parameters without a default value are required; parameters with a
    # default are optional.

    def example_tool(text: str, shout: bool = False) -> str:
        """One-line summary shown to the LLM as the tool description.

        Args:
            text: The text to transform.
            shout: If true, uppercase the result.
        """
        return text.upper() if shout else text

    cycle.tool_handler.register_tool(
        example_tool,
        # always_on=True means this tool's schema is sent to the LLM on
        # EVERY turn — expensive in tokens if you have many tools. Prefer
        # always_on=False and let the always-on `tools_search` tool (BM25
        # over tool name + description) enable it on demand when the model
        # asks for something matching. Only set True for tools the agent
        # truly needs available at all times.
        always_on=False,
        # 0-100. The caller's User.permission_level must be >= this to
        # invoke the tool. Default is 25. Raise it for anything
        # destructive (filesystem writes, shell exec, sysops-style tools).
        min_permission=25,
    )

    # async def tools are also supported and are awaited directly — use
    # async for I/O-bound work (HTTP calls, subprocess) so it doesn't block
    # the event loop. Sync functions instead run in a thread-pool executor.
    # Both are dispatched identically by ToolCallHandler.execute_tool_call.
    #
    # async def example_async_tool(url: str) -> str:
    #     """Fetch a URL and return its text.
    #
    #     Args:
    #         url: The URL to fetch.
    #     """
    #     ...
    # cycle.tool_handler.register_tool(example_async_tool)

    # ------------------------------------------------------------------
    # PROMPT PROVIDERS
    # ------------------------------------------------------------------
    # A prompt provider is a callable that returns a string (or None/"" to
    # contribute nothing this turn) to be injected into the system prompt
    # (or another role) EVERY turn. All system-role providers are
    # concatenated into a single system message during Context.assemble().
    # Keep these cheap since they run on every single turn.

    def _example_prompt(ctx) -> str:
        # ctx can be None at some call sites, and ctx.state may not have
        # "session" populated yet — always guard.
        session = ctx.state.get("session", {}) if ctx is not None else {}
        value = session.get(STATE_KEY)
        if not value:
            return ""
        return f"<example_module_state>{value}</example_module_state>"

    cycle.context.register_prompt(
        "example_module",   # unique id; re-registering the same id replaces the old provider
        _example_prompt,
        role="system",      # ROLE_SYSTEM by default
        # Pull from the merged cfg above (falls back to __init__.py's
        # default_config value if config.yaml doesn't override it) rather
        # than hard-coding the number here.
        priority=int(cfg.get("prompt_priority", 8)),
    )

    # ------------------------------------------------------------------
    # CONTEXT HOOKS
    # ------------------------------------------------------------------
    # Context.assemble() runs a pipeline of hooks each turn, in this order:
    #
    #   1. HOOK_PRE_ASSEMBLE_ASYNC  async fn(ctx) -> None
    #        Awaited by AgentCycle BEFORE assemble() is called (assemble()
    #        itself is synchronous). Use this for anything requiring
    #        `await` — DB reads/writes, network calls.
    #
    #   2. (DB history for the branch is loaded internally)
    #
    #   3. HOOK_PRE_ASSEMBLE         fn(ctx) -> None
    #        Sync, runs at the very start of assemble() (e.g. warm a cache).
    #
    #   4. HOOK_FILTER_TURN          fn(entry, age, ctx) -> bool
    #        Return False to drop a history turn entirely from the
    #        assembled message list.
    #
    #   5. HOOK_TRANSFORM_TURN       fn(entry, age, ctx) -> HistoryEntry | None
    #        Replace or compress a turn (e.g. summarize an old tool result
    #        to save tokens). Return None to leave it unchanged.
    #
    #   (adjacent same-role messages are merged automatically)
    #
    #   6. HOOK_POST_ASSEMBLE        fn(messages, ctx) -> list[dict] | None
    #        Final reshape of the fully assembled OpenAI-format message
    #        list. Return None to leave it unchanged.
    #
    #   (token budget trimming happens last, dropping oldest non-system turns)
    #
    # Register hooks against cycle.context — see context.py for the exact
    # registration method names/signatures (register_hook(HOOK_NAME, fn,
    # priority=...) as of this writing). Example (commented out — uncomment
    # and adapt if your module needs one):
    #
    # from TinyCTX.context import HOOK_FILTER_TURN
    #
    # def _drop_old_debug_turns(entry, age, ctx) -> bool:
    #     return not (age > 50 and "debug" in (entry.content or ""))
    #
    # cycle.context.register_hook(HOOK_FILTER_TURN, _drop_old_debug_turns, priority=10)

    # ------------------------------------------------------------------
    # SESSION STATE ("the State system")
    # ------------------------------------------------------------------
    # TinyCTX has no dedicated State class. "State" refers to two related
    # but distinct things:
    #
    # (a) SESSION STATE — a plain dict reconstructed by walking a
    #     conversation branch's ancestor chain in ConversationDB, merging
    #     each node's `state_delta` JSON column (most-recent node wins per
    #     key). This is how a value survives across multiple turns on the
    #     same branch without a dedicated database table. Real examples:
    #     modules/rag/__main__.py stores "rag_auto_targets" this way;
    #     modules/skills/__main__.py stores "skills_expanded_categories"
    #     this way. Both write it from a tool call and read it back in a
    #     prompt provider or hook on the next turn.
    #
    #     Read it (node_id is usually cycle.context.tail_node_id):
    #         state, depth = cycle.db.load_session_state(node_id)
    #         value = state.get(STATE_KEY)
    #
    #     Write it (call from inside a tool, where you have a node_id):
    #         cycle.db.update_node_state_delta(
    #             node_id, json.dumps({STATE_KEY: value})
    #         )
    #
    # (b) ctx.state — a plain dict attribute on the live Context object,
    #     scoped to a single assemble() call (cleared every time it runs).
    #     After assemble() runs, ctx.state["session"] holds the SAME
    #     session-state dict described in (a) — Context loads it
    #     internally via db.load_session_state() for convenience — plus
    #     bookkeeping keys like ctx.state["tokens_used"]. Hooks and prompt
    #     providers that only need to READ session state (not write it)
    #     should use ctx.state["session"] rather than calling
    #     load_session_state() themselves — see _example_prompt() above.
    #     ctx.state is never persisted; it's rebuilt from state_delta every
    #     single assemble() call. Only state_delta itself survives turns.

    def set_example_state(value: str) -> str:
        """Persist a value in session state so future turns on this branch can read it.

        Args:
            value: The value to remember.
        """
        cycle.db.update_node_state_delta(
            cycle.context.tail_node_id,
            json.dumps({STATE_KEY: value}),
        )
        return f"remembered: {value}"

    cycle.tool_handler.register_tool(set_example_state, always_on=False, min_permission=25)


# ======================================================================
# CHECKLIST FOR A NEW MODULE
# ======================================================================
# 1. Copy this folder to TinyCTX/modules/<name>/ (or custom_modules/<name>/
#    for a gitignored, user-local plugin).
# 2. Update __init__.py's EXTENSION_META: name, description, and
#    default_config for every tunable your module reads. Fix the
#    "from TinyCTX.modules.example_module import EXTENSION_META" line and
#    the "example_module" config-lookup key below to match your module's
#    actual package path and name.
# 3. Implement register_runtime and/or register_agent — delete the one you
#    don't need.
# 4. Register tools via cycle.tool_handler.register_tool(...). Default to
#    always_on=False; raise min_permission for anything destructive.
# 5. Register prompt providers via cycle.context.register_prompt(...) if
#    the agent needs standing context injected every turn.
# 6. Register context hooks via cycle.context.register_hook(...) only if
#    you need to filter/transform/reshape history or prompt assembly.
# 7. For cross-turn memory, use db.load_session_state /
#    db.update_node_state_delta with your own unique STATE_KEY — never
#    write to the built-in keys (platform, author_id, agent_name,
#    server_name, channel_name).
# 8. Run linters, and if this is a new top-level module, add it to the
#    project layout list in TinyCTX/CODEBASE.md.

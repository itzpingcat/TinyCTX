"""
runtime.py — Central runtime object. Owns DB, models, tool_handler, commands,
semaphore, SSE fanout, and module loading.

Runtime replaces Router + AgentLoop as the primary server-side object.
Bridges and the gateway talk to Runtime via push() and register_sse_handler().
Modules register via register(runtime: Runtime).

Public API
----------
Runtime(config)                    — construct; does not start background tasks
  await .start()                   — load modules, start cron/heartbeat tasks
  await .push(msg) -> bool         — accept an InboundMessage; False = at capacity (429)
  .abort(node_id) -> bool          — signal abort for an in-flight cycle
  .register_sse_handler(node_id, handler)   — attach an SSE fanout for a node
  .unregister_sse_handler(node_id, handler) — detach; unregisters cursor when last one gone
  await .shutdown()                — cancel all tasks

Attributes available to modules
--------------------------------
  .db            ConversationDB
  .config        Config
  .models        dict[str, LLM]
  .tool_handler  ToolCallHandler
  .commands      CommandRegistry
  .module_env    dict  — shared namespace for inter-module data

Module contract
---------------
  def register(runtime: Runtime) -> None:
      # Register prompt providers, hooks, tools, commands.
      # Called once at startup by Runtime.start().

Concurrency model
-----------------
Each InboundMessage with trigger=True spawns an asyncio.Task via
asyncio.create_task(). Concurrency is capped by a semaphore (max_workers).
At capacity push() returns False and the gateway sends 429. No idle workers.

Concurrent cycles on the same branch tail produce natural forks — two child
nodes written off the same parent. This is correct behaviour.

State delta / checkpoint
------------------------
push() reads session_delta_depth from context.state after assemble() runs
inside the cycle. If depth > config.checkpoint_threshold, push() writes a
full checkpoint state_delta on the triggering user node before spawning the
cycle. (The cycle itself calls assemble() which calls _load_state_from_db()
and reports the depth — Runtime reads it from context.state["session_delta_depth"]
after the first assemble in the cycle via the post_first_assemble hook.)

Because AgentCycle is sealed, the checkpoint is written by Runtime._process()
before the cycle runs: Runtime reads the current state by calling
context._load_state_from_db() directly on the wired context, then writes the
checkpoint delta onto the user node if needed.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from TinyCTX.config import Config, ModelConfig
from TinyCTX.contracts import (
    AgentEvent,
    InboundMessage,
    Platform,
    UserIdentity,
)
from TinyCTX.utils.attachments import save_upload as _save_upload
from TinyCTX.context import Context, HOOK_PRE_ASSEMBLE_ASYNC
from TinyCTX.cycle import AgentCycle, CycleHooks
from TinyCTX.db import ConversationDB
from TinyCTX.ai import LLM
from TinyCTX.utils.tool_handler import ToolCallHandler
from TinyCTX.utils.commands import CommandRegistry

logger = logging.getLogger(__name__)

MODULES_DIR = Path(__file__).parent / "modules"

EventHandler = Callable[[AgentEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# ContextProxy — shim so modules can call runtime.context.register_prompt()
# and runtime.context.register_hook() during register(), exactly as they did
# with AgentLoop.context. Calls are forwarded to runtime-level storage and
# replayed onto every new cycle Context in _make_context().
# ---------------------------------------------------------------------------

class _ContextProxy:
    """Proxy exposing the subset of Context's API that modules call at register time."""

    def __init__(self, runtime: "Runtime") -> None:
        self._rt = runtime

    def register_prompt(self, pid: str, provider, *, role: str = "system", priority: int = 0) -> None:
        self._rt._prompt_registrations.append((pid, provider, role, priority))

    def register_hook(self, stage: str, fn, *, priority: int = 0) -> None:
        self._rt._hook_registrations.append((stage, fn, priority))

    # Modules that read agent.context.state / agent.context.dialogue at
    # runtime (inside hooks, not at register time) get an empty fallback.
    # The real state lives on each cycle's Context; these are only accessed
    # during register() setup where no cycle is active.
    @property
    def state(self) -> dict:
        return {}

    @property
    def dialogue(self) -> list:
        return []

    @property
    def tail_node_id(self):
        return None


# ---------------------------------------------------------------------------
# LLM construction helper
# ---------------------------------------------------------------------------

def _build_llm(cfg: ModelConfig) -> LLM:
    try:
        api_key = cfg.api_key
    except EnvironmentError:
        api_key = "no-key"
    return LLM(
        base_url=cfg.base_url,
        api_key=api_key,
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        budget_tokens=cfg.budget_tokens,
        reasoning_effort=cfg.reasoning_effort,
        cache_prompts=cfg.cache_prompts,
    )


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class Runtime:
    def __init__(self, config: Config) -> None:
        self.config = config

        # Open shared DB
        workspace = Path(config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self.db = ConversationDB(workspace / "agent.db")

        # Pre-build all non-embedding LLM instances
        self.models: dict[str, LLM] = {
            name: _build_llm(mc)
            for name, mc in config.models.items()
            if not mc.is_embedding
        }

        # Shared tool handler and command registry
        self.tool_handler = ToolCallHandler()
        self.tool_handler.register_tool(self.tool_handler.tools_search, always_on=True)
        self.commands = CommandRegistry()

        # Inter-module shared namespace (e.g. librarian handle, playwright instance)
        self.module_env: dict[str, Any] = {}

        # SSE fanout: node_id → list of per-request queues
        self._sse_queues: dict[str, list[asyncio.Queue]] = {}
        # Cursor handlers registered with legacy Router-compat dispatch
        self._cursor_handlers: dict[str, EventHandler] = {}
        # Platform → handler (for non-cursor fallback dispatch)
        self._platform_handlers: dict[str, EventHandler] = {}
        # node_id → platform value (for dispatch)
        self._node_platforms: dict[str, str] = {}

        # Concurrency cap
        max_workers = getattr(config, "max_workers", 8)
        self._semaphore = asyncio.Semaphore(max_workers)
        self._active: int = 0  # tracked manually for fast capacity check

        # Snapshot of always-on tools (registered before modules load).
        # Used by _apply_enabled_tools_from_state to preserve them on restore.
        self._initial_enabled_tools: frozenset[str] = frozenset(self.tool_handler.enabled)

        # Abort signals: node_id → Event
        self._abort_events: dict[str, asyncio.Event] = {}

        # Background tasks spawned by push() / start()
        self._tasks: set[asyncio.Task] = set()

        # Post-turn hooks registered by modules
        # Signature: async (tail_node_id: str) -> None
        self._post_turn_hooks: list[Callable[[str], Awaitable[None]]] = []

        # Pre-assemble hooks stored per-context are registered by modules via
        # the Context they get from _make_context(). Runtime keeps a list of
        # hook factories so each new cycle context gets the same hooks.
        # Signature: async (ctx: Context) -> None
        self._pre_assemble_hook_factories: list[Callable] = []

        # Checkpoint threshold: if state delta walk depth exceeds this,
        # write a full checkpoint on the triggering node.
        self._checkpoint_threshold: int = getattr(config, "checkpoint_threshold", 20)

        # Module-registered prompts and hooks — replayed onto every new cycle context.
        # Populated by _ContextProxy during module register() calls.
        self._prompt_registrations: list[tuple] = []  # (pid, provider, role, priority)
        self._hook_registrations:   list[tuple] = []  # (stage, fn, priority)

        # Expose a context proxy so old-style modules can call
        # runtime.context.register_prompt() / runtime.context.register_hook().
        self.context = _ContextProxy(self)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load modules and start singleton background tasks."""
        self._load_modules()
        logger.info("Runtime started (%d models, %d tools)",
                    len(self.models), len(self.tool_handler.tools))

    def _load_modules(self) -> None:
        """Scan modules/ and call register(runtime) on each."""
        if not MODULES_DIR.exists():
            return
        for entry in sorted(MODULES_DIR.iterdir()):
            if not entry.is_dir():
                continue
            if not ((entry / "__main__.py").exists() or (entry / "__init__.py").exists()):
                continue
            module_name = f"TinyCTX.modules.{entry.name}"
            try:
                mod = None
                for suffix in (".__main__", ""):
                    try:
                        candidate = importlib.import_module(module_name + suffix)
                        if hasattr(candidate, "register"):
                            mod = candidate
                            break
                    except ModuleNotFoundError:
                        continue
                if mod is None:
                    logger.warning("Module '%s' has no register() — skipping", entry.name)
                    continue
                mod.register(self)
                logger.info("Loaded module '%s'", entry.name)
            except Exception:
                logger.exception("Failed to load module '%s'", entry.name)

    # ------------------------------------------------------------------
    # Module registration helpers (called by modules inside register())
    # ------------------------------------------------------------------

    def register_background_hook(self, fn: Callable[[str], Awaitable[None]]) -> None:
        """Register an async post-turn hook: async (tail_node_id: str) -> None."""
        self._post_turn_hooks.append(fn)

    def register_pre_assemble_hook(self, fn: Callable) -> None:
        """
        Register an async pre-assemble hook factory.
        fn will be registered on every Context created for a new cycle.
        Signature: async (ctx: Context) -> None
        """
        self._pre_assemble_hook_factories.append(fn)

    # ------------------------------------------------------------------
    # SSE fanout (gateway calls these)
    # ------------------------------------------------------------------

    def register_sse_handler(self, node_id: str, queue: asyncio.Queue) -> None:
        """
        Attach an SSE response queue to node_id. Multiple queues per node_id
        are supported (multiple concurrent SSE clients on the same cursor).
        Registers a cursor handler on first queue for this node_id.
        """
        if node_id not in self._sse_queues:
            self._sse_queues[node_id] = []
            self._cursor_handlers[node_id] = self._make_fanout_handler(node_id)
        self._sse_queues[node_id].append(queue)

    def unregister_sse_handler(self, node_id: str, queue: asyncio.Queue) -> None:
        """Remove one SSE queue. Cleans up cursor handler when last queue is gone."""
        queues = self._sse_queues.get(node_id, [])
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            self._sse_queues.pop(node_id, None)
            self._cursor_handlers.pop(node_id, None)

    def _make_fanout_handler(self, node_id: str) -> EventHandler:
        async def _handler(event: AgentEvent) -> None:
            for q in list(self._sse_queues.get(node_id, [])):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("SSE queue full for node_id=%s — dropping event", node_id)
        return _handler

    # ------------------------------------------------------------------
    # Platform / cursor handler registration (bridge-compat)
    # ------------------------------------------------------------------

    def register_platform_handler(self, platform: str, handler: EventHandler) -> None:
        self._platform_handlers[platform] = handler

    async def _dispatch_event(self, node_id: str, event: AgentEvent) -> None:
        """Dispatch one event: cursor handler > platform handler > drop."""
        handler = self._cursor_handlers.get(node_id)
        if handler is not None:
            try:
                await handler(event)
            except Exception:
                logger.exception("Cursor handler raised for node_id=%s", node_id)
            return
        platform = self._node_platforms.get(node_id)
        if platform:
            h = self._platform_handlers.get(platform)
            if h:
                try:
                    await h(event)
                except Exception:
                    logger.exception("Platform handler raised for platform=%s", platform)
                return
        logger.debug("No handler for node_id=%s — event dropped", node_id)

    # ------------------------------------------------------------------
    # Abort
    # ------------------------------------------------------------------

    def abort(self, node_id: str) -> bool:
        """Signal abort for any in-flight cycle on node_id."""
        ev = self._abort_events.get(node_id)
        if ev is None:
            return False
        ev.set()
        logger.info("Abort signalled for node_id=%s", node_id)
        return True

    def _get_abort_event(self, node_id: str) -> asyncio.Event:
        if node_id not in self._abort_events:
            self._abort_events[node_id] = asyncio.Event()
        else:
            self._abort_events[node_id].clear()
        return self._abort_events[node_id]

    # ------------------------------------------------------------------
    # push() — main entry point for bridges
    # ------------------------------------------------------------------

    async def push(self, msg: InboundMessage) -> bool:
        """
        Accept an InboundMessage.

        Non-trigger messages (msg.trigger=False) are persisted as nodes
        immediately and return True without spawning a cycle.

        Trigger messages spawn an asyncio.Task if under capacity.
        Returns False (429) if at capacity.
        """
        # Record platform for event dispatch
        self._node_platforms[msg.tail_node_id] = msg.author.platform.value

        # Save any attachments to disk and collect their paths.
        attachment_paths_json: str | None = None
        if msg.attachments:
            workspace = Path(self.config.workspace.path).expanduser().resolve()
            uploads_dir = workspace / self.config.attachments.uploads_dir
            saved_paths: list[str] = []
            for att in msg.attachments:
                try:
                    p = _save_upload(att, uploads_dir)
                    saved_paths.append(str(p))
                except Exception:
                    logger.exception("push(): failed to save attachment '%s'", att.filename)
            if saved_paths:
                attachment_paths_json = json.dumps(saved_paths, ensure_ascii=False)

        # Compute state delta for this node (identity fields that changed).
        state_delta = self._compute_state_delta(msg)

        # Persist the inbound node immediately regardless of trigger.
        user_node = self.db.add_node(
            parent_id=msg.tail_node_id,
            role="user",
            content=msg.text,
            author_id=msg.author.user_id,
            author_name=msg.author.username,
            attachment_paths=attachment_paths_json,
            state_delta=json.dumps(state_delta, ensure_ascii=False) if state_delta else None,
        )
        new_tail = user_node.id

        if not msg.trigger:
            logger.debug("Non-trigger message persisted as node %s", new_tail)
            return True

        # Capacity check
        max_workers = self._semaphore._value + self._active  # total slots
        if self._active >= max_workers:
            logger.warning("Runtime at capacity (%d/%d) — rejecting push for %s",
                           self._active, max_workers, new_tail)
            return False

        # Compute state delta and conditionally write checkpoint onto the user node
        self._maybe_write_checkpoint(new_tail, msg)

        abort_event = self._get_abort_event(new_tail)
        task = asyncio.create_task(
            self._process(msg, new_tail, abort_event),
            name=f"cycle:{new_tail}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    def _compute_state_delta(self, msg: InboundMessage) -> dict:
        """
        Build a minimal state delta for the inbound user node.

        Only includes keys that differ from the previously assembled session
        state. On the very first message there is no prior state, so all
        identity fields are written (forming the implicit first checkpoint).

        enabled_tools is included whenever the current set differs from what
        the last delta recorded — it stores the full list on each change, not
        a diff.
        """
        # Read what the current branch's state looks like *before* this node
        # is appended, by walking from the parent (msg.tail_node_id).
        ctx = self._make_context(msg.tail_node_id)
        prior_state, _ = ctx._load_state_from_db()

        delta: dict = {}

        # Identity fields — only write when changed.
        def _maybe(key: str, value) -> None:
            if prior_state.get(key) != value:
                delta[key] = value

        _maybe("platform",       msg.author.platform.value)
        _maybe("author_id",      msg.author.user_id)
        _maybe("author_name",    msg.author.username)
        _maybe("permission_level", msg.permission_level)
        if msg.server_name is not None:
            _maybe("server_name", msg.server_name)
        if msg.channel_name is not None:
            _maybe("channel_name", msg.channel_name)

        # enabled_tools — reflect current runtime state.
        current_tools = sorted(self.tool_handler.enabled)
        if sorted(prior_state.get("enabled_tools") or []) != current_tools:
            delta["enabled_tools"] = current_tools

        return delta

    def _apply_enabled_tools_from_state(self, state: dict) -> None:
        """
        Sync tool_handler.enabled from replayed session state.
        Called inside _run_cycle() after context.assemble() has rebuilt state.

        Preserves always_on tools (those registered with always_on=True are in
        the initial enabled set and must never be removed).
        """
        tools_from_state = state.get("enabled_tools")
        if tools_from_state is None:
            return  # no prior record — leave current set unchanged
        always_on = {
            name for name, tool in self.tool_handler.tools.items()
            if name in self.tool_handler.enabled
            and name not in self._initial_enabled_tools
        }
        # Reconstruct: union of state list + always_on tools
        self.tool_handler.enabled = set(tools_from_state) | self._initial_enabled_tools

    def _maybe_write_checkpoint(self, node_id: str, msg: InboundMessage) -> None:
        """
        Walk the state delta chain from node_id. If the walk depth exceeds
        checkpoint_threshold, write a full checkpoint state_delta on node_id.
        """
        # Build a throwaway context wired to the DB to reuse _load_state_from_db.
        ctx = self._make_context(node_id)
        state, depth = ctx._load_state_from_db()
        if depth <= self._checkpoint_threshold:
            return

        # Build the full checkpoint delta from the session state plus current msg fields.
        checkpoint: dict = {
            "_checkpoint":    True,
            "platform":       msg.author.platform.value,
            "author_id":      msg.author.user_id,
            "author_name":    msg.author.username,
            "permission_level": msg.permission_level,
        }
        if msg.server_name is not None:
            checkpoint["server_name"] = msg.server_name
        if msg.channel_name is not None:
            checkpoint["channel_name"] = msg.channel_name
        # Merge in any existing state keys not already set above.
        for k, v in state.items():
            if k not in checkpoint:
                checkpoint[k] = v

        # Write checkpoint as state_delta on the user node we just created.
        self.db.update_node_state_delta(node_id, json.dumps(checkpoint, ensure_ascii=False))
        logger.debug(
            "Checkpoint written on node %s (walk depth was %d)", node_id, depth
        )

    # ------------------------------------------------------------------
    # _process() — runs inside a task
    # ------------------------------------------------------------------

    async def _process(
        self,
        msg: InboundMessage,
        tail_node_id: str,
        abort_event: asyncio.Event,
    ) -> None:
        """
        Acquire the semaphore, construct and run an AgentCycle, dispatch events.
        Exits when the cycle finishes; task is then garbage collected.
        """
        async with self._semaphore:
            self._active += 1
            try:
                await self._run_cycle(msg, tail_node_id, abort_event)
            except Exception:
                logger.exception("_process raised for tail=%s", tail_node_id)
            finally:
                self._active -= 1

    async def _run_cycle(
        self,
        msg: InboundMessage,
        tail_node_id: str,
        abort_event: asyncio.Event,
    ) -> None:
        ctx = self._make_context(tail_node_id)

        # The user node was already written by push() — set tail to it so the
        # cycle's context sees it as the branch tip.
        ctx.set_tail(tail_node_id)

        # Replay session state to sync tool_handler.enabled from the branch.
        # This makes enabled_tools durable and branchable: rewinding a cursor
        # restores the exact tool set that was active at that point.
        session_state, _ = ctx._load_state_from_db()
        self._apply_enabled_tools_from_state(session_state)

        cycle = AgentCycle(
            tail_node_id=tail_node_id,
            context=ctx,
            models=self.models,
            tool_handler=self.tool_handler,
            config=self.config,
            abort_event=abort_event,
            permission_level=msg.permission_level,
            hooks=CycleHooks(post_turn=list(self._post_turn_hooks)),
            message_id=msg.message_id,
            trace_id=msg.trace_id,
        )

        # Run the cycle, passing msg=None because push() already persisted the
        # user node — the cycle should skip Stage 1 (intake) and go straight
        # to assembly/inference.
        async for event in cycle.run(msg=None):
            await self._dispatch_event(tail_node_id, event)

    def _make_context(self, tail_node_id: str) -> Context:
        """Construct and wire a Context for a new cycle or throwaway state read."""
        primary_mc = self.config.models.get(self.config.llm.primary)
        ctx = Context(
            token_limit=self.config.context,
            image_tokens_per_block=primary_mc.tokens_per_image if primary_mc else 280,
        )
        ctx.set_db(self.db)
        ctx.set_tail(tail_node_id)
        # Register all pre-assemble hook factories onto this context.
        for fn in self._pre_assemble_hook_factories:
            ctx.register_hook(HOOK_PRE_ASSEMBLE_ASYNC, fn)
        # Replay module prompt registrations (from _ContextProxy calls at startup).
        for pid, provider, role, priority in self._prompt_registrations:
            ctx.register_prompt(pid, provider, role=role, priority=priority)
        # Replay module sync hook registrations.
        for stage, fn, priority in self._hook_registrations:
            ctx.register_hook(stage, fn, priority=priority)
        return ctx

    # ------------------------------------------------------------------
    # Helpers for modules that need to push background messages
    # ------------------------------------------------------------------

    async def push_background(self, tail_node_id: str, *, content: str = "") -> bool:
        """
        Push a synthetic InboundMessage (trigger=True) onto a branch node.
        Used by background hooks (e.g. knowledge consolidation).
        Background cycles receive CycleHooks(post_turn=[]) automatically
        because this path bypasses the module post_turn hooks list.
        """
        abort_event = self._get_abort_event(tail_node_id)
        task = asyncio.create_task(
            self._run_background_cycle(tail_node_id, abort_event),
            name=f"bg-cycle:{tail_node_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run_background_cycle(
        self,
        tail_node_id: str,
        abort_event: asyncio.Event,
    ) -> None:
        ctx = self._make_context(tail_node_id)
        cycle = AgentCycle(
            tail_node_id=tail_node_id,
            context=ctx,
            models=self.models,
            tool_handler=self.tool_handler,
            config=self.config,
            abort_event=abort_event,
            permission_level=100,           # background = full internal permission
            hooks=CycleHooks(post_turn=[]), # no chaining
            message_id="synthetic",
            trace_id=str(uuid.uuid4()),
        )
        async for _ in cycle.run(msg=None):
            pass  # background events are discarded

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Cancel all in-flight tasks and close the DB."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self.db.close()
        logger.info("Runtime shutdown complete.")

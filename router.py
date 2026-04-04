"""
router.py — Async lane router keyed by node_id (cursor).
No LLM calls, no tools, no memory. Pure routing and async primitives.

Phase 2 tree refactor
---------------------
Lanes are keyed by tail_node_id (str) instead of SessionKey.
InboundMessage.tail_node_id is the sole routing key.
SessionKey and ChatType are gone.

Group chat routing
------------------
GroupLane wraps Lane and applies GroupPolicy: trigger detection, text
stripping, non-trigger buffering, and optional timeout flush.

Abort
-----
Lane.abort_event is checked by AgentLoop between cycles and mid-stream.
Router.abort_generation(node_id) sets it; the drain worker clears it at
the start of each new turn so it doesn't bleed forward.

Synthetic turns
---------------
Router.push_synthetic(node_id) enqueues None to the lane. The drain worker
calls loop.run(None) which skips Stage 1 (no user intake).

Command registry
----------------
Router.commands is a CommandRegistry. Modules register slash commands at
register() time via agent.commands (agent.commands is the same object).
Bridges call router.commands.dispatch(text, context) before pushing to the
router; dispatch() returns True if handled so the bridge can skip push().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from contracts import (
    InboundMessage, AgentEvent,
    GroupPolicy, ActivationMode,
)
from agent import AgentLoop
from config import Config
from utils.commands import CommandRegistry

logger = logging.getLogger(__name__)

EventHandler = Callable[[AgentEvent], Awaitable[None]]

LANE_QUEUE_MAX = 32


@dataclass
class Lane:
    node_id:       str
    config:        Config
    event_handler: EventHandler
    router:        "Router"

    loop:        AgentLoop     = field(init=False)
    queue:       asyncio.Queue = field(init=False)
    abort_event: asyncio.Event = field(init=False)
    _worker:     asyncio.Task | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.queue       = asyncio.Queue(maxsize=LANE_QUEUE_MAX)
        self.abort_event = asyncio.Event()
        self.loop        = AgentLoop(
            tail_node_id=self.node_id,
            config=self.config,
        )
        self.loop.gateway = self.router
        # Expose the shared command registry to the agent loop so modules can
        # register commands at register() time via agent.commands.
        self.loop.commands = self.router.commands
        # Load modules NOW — after commands is wired — so modules that call
        # agent.commands.register() see the shared router registry, not the
        # throwaway CommandRegistry created in AgentLoop.__init__.
        self.loop.load_modules()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain(), name=f"lane:{self.node_id}"
            )

    async def enqueue(self, msg: InboundMessage | None) -> bool:
        """Enqueue a message (or None for a synthetic turn). Returns False if full."""
        try:
            self.queue.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "Lane %s queue full (%d/%d) — dropping",
                self.node_id, self.queue.qsize(), LANE_QUEUE_MAX,
            )
            return False

    def abort(self) -> None:
        self.abort_event.set()
        logger.info("Lane %s: abort signalled", self.node_id)

    async def _drain(self) -> None:
        while True:
            msg = await self.queue.get()
            self.abort_event.clear()  # fresh slate for every turn
            try:
                async for event in self.loop.run(msg, abort_event=self.abort_event):
                    try:
                        await self.event_handler(event)
                    except Exception:
                        logger.exception("Event handler raised for %s", self.node_id)
            except Exception:
                logger.exception("AgentLoop raised for %s — lane continues", self.node_id)
            finally:
                self.queue.task_done()

    def reset(self) -> None:
        self.loop.reset()  # clears in-memory context; tree in agent.db is preserved

    async def stop(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# GroupLane helpers
# ---------------------------------------------------------------------------

def _gp_strip_trigger(text: str, policy: GroupPolicy) -> str:
    if policy.bot_mxid:
        text = text.replace(policy.bot_mxid, "")
    if policy.bot_localpart:
        text = text.replace(f"@{policy.bot_localpart}", "")
    if text.startswith(policy.trigger_prefix):
        text = text[len(policy.trigger_prefix):]
    return text.strip()


def _gp_replace_text(msg: InboundMessage, new_text: str) -> InboundMessage:
    return InboundMessage(
        tail_node_id=msg.tail_node_id,
        author=msg.author,
        content_type=msg.content_type,
        text=new_text,
        message_id=msg.message_id,
        timestamp=msg.timestamp,
        reply_to_id=msg.reply_to_id,
        attachments=msg.attachments,
        trace_id=msg.trace_id,
        group_policy=msg.group_policy,
    )


# ---------------------------------------------------------------------------
# GroupLane
# ---------------------------------------------------------------------------

class GroupLane:
    """Wraps Lane for group conversations, applying GroupPolicy."""

    def __init__(self, lane: Lane, policy: GroupPolicy) -> None:
        self._lane   = lane
        self._policy = policy
        self._buffer: list[InboundMessage] = []
        self._timeout_task: asyncio.Task | None = None

    async def push(self, msg: InboundMessage) -> bool:
        p    = self._policy
        text = msg.text.strip()

        if p.activation == ActivationMode.ALWAYS:
            return await self._lane.enqueue(msg)

        if not self._is_trigger(text, p):
            self._buffer.append(msg)
            if p.buffer_timeout_s > 0:
                self._reset_timeout()
            return True

        stripped  = _gp_strip_trigger(text, p)
        trigger   = _gp_replace_text(msg, stripped)
        combined  = self._flush_buffer(trigger)
        return await self._lane.enqueue(combined)

    def reset(self) -> None:
        self._buffer.clear()
        self._cancel_timeout()
        self._lane.reset()

    async def stop(self) -> None:
        self._cancel_timeout()
        await self._lane.stop()

    def abort(self) -> None:
        self._lane.abort()

    @property
    def queue(self):        return self._lane.queue
    @property
    def loop(self):         return self._lane.loop
    @property
    def abort_event(self):  return self._lane.abort_event
    @property
    def node_id(self):      return self._lane.node_id

    def set_activation(self, mode: ActivationMode) -> None:
        p = self._policy
        self._policy = GroupPolicy(
            activation=mode,
            trigger_prefix=p.trigger_prefix,
            bot_mxid=p.bot_mxid,
            bot_localpart=p.bot_localpart,
            buffer_timeout_s=p.buffer_timeout_s,
        )

    def _is_trigger(self, text: str, p: GroupPolicy) -> bool:
        if p.activation == ActivationMode.PREFIX:
            return text.startswith(p.trigger_prefix)
        if text.startswith(p.trigger_prefix):
            return True
        if p.bot_mxid and p.bot_mxid in text:
            return True
        if p.bot_localpart and f"@{p.bot_localpart}" in text:
            return True
        return False

    def _flush_buffer(self, trigger: InboundMessage) -> InboundMessage:
        self._cancel_timeout()
        buffered      = self._buffer[:]
        self._buffer  = []
        if not buffered:
            return trigger
        lines = [f"[{m.author.username}]: {m.text}" for m in buffered]
        lines.append(trigger.text)
        return _gp_replace_text(trigger, "\n".join(lines))

    def _reset_timeout(self) -> None:
        self._cancel_timeout()
        self._timeout_task = asyncio.create_task(
            self._timeout_flush(), name=f"group-timeout:{self._lane.node_id}"
        )

    def _cancel_timeout(self) -> None:
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._timeout_task = None

    async def _timeout_flush(self) -> None:
        try:
            await asyncio.sleep(self._policy.buffer_timeout_s)
            if not self._buffer:
                return
            last     = self._buffer[-1]
            combined = self._flush_buffer(last)
            await self._lane.enqueue(combined)
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# _LaneRouter
# ---------------------------------------------------------------------------

class _LaneRouter:
    def __init__(self, config: Config, event_handler: EventHandler, router: "Router") -> None:
        self._config        = config
        self._event_handler = event_handler
        self._router        = router
        self._lanes: dict[str, "Lane | GroupLane"] = {}  # node_id → lane

    def ensure_lane(self, node_id: str) -> "Lane":
        """Return the lane for node_id, creating it if needed. No work enqueued."""
        if node_id not in self._lanes:
            lane = Lane(
                node_id=node_id,
                config=self._config,
                event_handler=self._event_handler,
                router=self._router,
            )
            lane.start()
            self._lanes[node_id] = lane
            logger.info("Opened lane %s (eager)", node_id)
        return self._lanes[node_id]  # type: ignore[return-value]

    async def route(self, msg: InboundMessage) -> bool:
        lane = self._get_or_create(msg.tail_node_id, msg)
        if isinstance(lane, GroupLane):
            return await lane.push(msg)
        return await lane.enqueue(msg)

    async def enqueue_synthetic(self, node_id: str) -> bool:
        """Enqueue a synthetic turn (None msg) for an existing or new lane."""
        if node_id not in self._lanes:
            lane = Lane(
                node_id=node_id,
                config=self._config,
                event_handler=self._event_handler,
                router=self._router,
            )
            lane.start()
            self._lanes[node_id] = lane
            logger.info("Opened lane %s (synthetic)", node_id)
        lane = self._lanes[node_id]
        inner = lane._lane if isinstance(lane, GroupLane) else lane
        return await inner.enqueue(None)

    def _get_or_create(self, node_id: str, msg: InboundMessage) -> "Lane | GroupLane":
        if node_id not in self._lanes:
            inner = Lane(
                node_id=node_id,
                config=self._config,
                event_handler=self._event_handler,
                router=self._router,
            )
            inner.start()
            if msg.group_policy is not None:
                lane: "Lane | GroupLane" = GroupLane(inner, msg.group_policy)
                logger.info("Opened GroupLane %s (activation=%s)", node_id, msg.group_policy.activation)
            else:
                lane = inner
                logger.info("Opened lane %s", node_id)
            self._lanes[node_id] = lane
        return self._lanes[node_id]

    async def close_all(self) -> None:
        for lane in self._lanes.values():
            await lane.stop()
        self._lanes.clear()

    @property
    def active_lanes(self) -> list[str]:
        return list(self._lanes.keys())


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Router:
    def __init__(self, config: Config) -> None:
        self._config             = config
        self._platform_handlers: dict[str, EventHandler] = {}
        self._cursor_handlers:   dict[str, EventHandler] = {}  # node_id → handler
        self._node_platforms:    dict[str, str]           = {}  # node_id → platform value
        # Shared command registry — modules register commands, bridges dispatch.
        self.commands            = CommandRegistry()
        self._lane_router        = _LaneRouter(
            config=config,
            event_handler=self._dispatch_event,
            router=self,
        )

    def register_platform_handler(self, platform: str, handler: EventHandler) -> None:
        self._platform_handlers[platform] = handler
        logger.info("Registered platform handler for '%s'", platform)

    def register_reply_handler(self, platform: str, handler: EventHandler) -> None:
        self.register_platform_handler(platform, handler)

    def register_cursor_handler(self, node_id: str, handler: EventHandler) -> None:
        """Register a per-cursor event handler (replaces register_session_handler)."""
        self._cursor_handlers[node_id] = handler

    def unregister_cursor_handler(self, node_id: str) -> None:
        self._cursor_handlers.pop(node_id, None)

    async def push(self, msg: InboundMessage) -> bool:
        # Record which platform this cursor belongs to for event dispatch.
        self._node_platforms[msg.tail_node_id] = msg.author.platform.value
        return await self._lane_router.route(msg)

    async def push_synthetic(self, node_id: str) -> bool:
        """Queue a generation with no new user message (run(None))."""
        return await self._lane_router.enqueue_synthetic(node_id)

    def open_lane(self, node_id: str, platform: str) -> None:
        """
        Eagerly open a lane for node_id without enqueuing any work.

        Creates the Lane (which triggers load_modules() and populates the
        shared command registry) and registers the platform so events from
        the lane can be dispatched. Safe to call multiple times — a no-op
        if the lane already exists.
        """
        self._node_platforms[node_id] = platform
        self._lane_router.ensure_lane(node_id)

    async def _dispatch_event(self, event: AgentEvent) -> None:
        # Use lane_node_id (stable original cursor) for dispatch, not tail_node_id
        # (which advances as new DB nodes are written during the turn).
        lane_id = event.lane_node_id

        handler = self._cursor_handlers.get(lane_id)
        if handler is not None:
            try:
                await handler(event)
            except Exception:
                logger.exception("Cursor handler failed for %s", event.trace_id)
            return

        platform = self._node_platforms.get(lane_id)
        if platform is None:
            logger.error("Cannot determine platform for lane %s — dropping %s", lane_id, event.trace_id)
            return
        handler = self._platform_handlers.get(platform)
        if handler is None:
            logger.error("No handler for platform '%s' — dropping %s", platform, event.trace_id)
            return
        try:
            await handler(event)
        except Exception:
            logger.exception("Platform handler failed for %s", event.trace_id)

    def reset_lane(self, node_id: str) -> None:
        lane = self._lane_router._lanes.get(node_id)
        if lane:
            lane.reset()

    def abort_generation(self, node_id: str) -> bool:
        lane = self._lane_router._lanes.get(node_id)
        if lane is None:
            return False
        lane.abort()
        return True

    def set_group_activation(self, node_id: str, mode: "str | ActivationMode") -> bool:
        if isinstance(mode, str):
            try:
                mode = ActivationMode(mode)
            except ValueError:
                logger.error("set_group_activation: unknown mode %r", mode)
                return False
        lane = self._lane_router._lanes.get(node_id)
        if isinstance(lane, GroupLane):
            lane.set_activation(mode)
            return True
        return False

    async def shutdown(self) -> None:
        await self._lane_router.close_all()

    @property
    def active_lanes(self) -> list[str]:
        """node_ids of all currently-open lanes."""
        return self._lane_router.active_lanes

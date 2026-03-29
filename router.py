"""
router.py — Async session router and lane queues.
No LLM calls, no tools, no memory. Pure routing and async primitives.

Group chat routing
------------------
GroupLane wraps Lane and applies GroupPolicy: trigger detection, text
stripping, non-trigger buffering, and optional timeout flush.

Abort
-----
Lane.abort_event is checked by AgentLoop between cycles and mid-stream.
Router.abort_generation(key) sets it; the drain worker clears it at the
start of each new turn so it doesn't bleed forward.

Synthetic turns
---------------
Router.push_synthetic(key) enqueues None to the lane. The drain worker
calls loop.run(None) which skips Stage 1 (no user intake).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from contracts import (
    InboundMessage, AgentEvent, SessionKey, ChatType,
    GroupPolicy, ActivationMode,
)
from agent import AgentLoop
from config import Config

logger = logging.getLogger(__name__)

EventHandler = Callable[[AgentEvent], Awaitable[None]]

LANE_QUEUE_MAX = 32


@dataclass
class Lane:
    session_key:      SessionKey
    config:           Config
    event_handler:    EventHandler
    router:           "Router"
    version_override: int | None = None

    loop:        AgentLoop     = field(init=False)
    queue:       asyncio.Queue = field(init=False)
    abort_event: asyncio.Event = field(init=False)
    _worker:     asyncio.Task | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.queue       = asyncio.Queue(maxsize=LANE_QUEUE_MAX)
        self.abort_event = asyncio.Event()
        self.loop        = AgentLoop(
            session_key=self.session_key,
            config=self.config,
            version_override=self.version_override,
        )
        self.loop.gateway = self.router

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain(), name=f"lane:{self.session_key}"
            )

    async def enqueue(self, msg: InboundMessage | None) -> bool:
        """Enqueue a message (or None for a synthetic turn). Returns False if full."""
        try:
            self.queue.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "Lane %s queue full (%d/%d) — dropping",
                self.session_key, self.queue.qsize(), LANE_QUEUE_MAX,
            )
            return False

    def abort(self) -> None:
        self.abort_event.set()
        logger.info("Lane %s: abort signalled", self.session_key)

    async def _drain(self) -> None:
        while True:
            msg = await self.queue.get()
            self.abort_event.clear()  # fresh slate for every turn
            try:
                async for event in self.loop.run(msg, abort_event=self.abort_event):
                    try:
                        await self.event_handler(event)
                    except Exception:
                        logger.exception("Event handler raised for %s", self.session_key)
            except Exception:
                logger.exception("AgentLoop raised for %s — lane continues", self.session_key)
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
        session_key=msg.session_key,
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
    """Wraps Lane for group sessions, applying GroupPolicy."""

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
    def session_key(self):  return self._lane.session_key

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
            self._timeout_flush(), name=f"group-timeout:{self._lane.session_key}"
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
# _SessionRouter
# ---------------------------------------------------------------------------

class _SessionRouter:
    def __init__(self, config: Config, event_handler: EventHandler, router: "Router") -> None:
        self._config        = config
        self._event_handler = event_handler
        self._router        = router
        self._lanes: dict[SessionKey, "Lane | GroupLane"] = {}
        self._version_overrides: dict[SessionKey, int] = {}

    async def route(self, msg: InboundMessage) -> bool:
        lane = self._get_or_create(msg.session_key, msg)
        if isinstance(lane, GroupLane):
            return await lane.push(msg)
        return await lane.enqueue(msg)

    async def enqueue_synthetic(self, key: SessionKey) -> bool:
        """Enqueue a synthetic turn (None msg) for an existing or new lane."""
        if key not in self._lanes:
            lane = Lane(
                session_key=key,
                config=self._config,
                event_handler=self._event_handler,
                router=self._router,
                version_override=self._version_overrides.pop(key, None),
            )
            lane.start()
            self._lanes[key] = lane
            logger.info("Opened lane %s (synthetic)", key)
        lane = self._lanes[key]
        inner = lane._lane if isinstance(lane, GroupLane) else lane
        return await inner.enqueue(None)

    def _get_or_create(self, key: SessionKey, msg: InboundMessage) -> "Lane | GroupLane":
        if key not in self._lanes:
            version_override = self._version_overrides.pop(key, None)
            inner = Lane(
                session_key=key,
                config=self._config,
                event_handler=self._event_handler,
                router=self._router,
                version_override=version_override,
            )
            inner.start()
            if key.chat_type == ChatType.GROUP and msg.group_policy is not None:
                lane: "Lane | GroupLane" = GroupLane(inner, msg.group_policy)
                logger.info("Opened GroupLane %s (activation=%s)", key, msg.group_policy.activation)
            else:
                lane = inner
                logger.info("Opened lane %s", key)
            self._lanes[key] = lane
        return self._lanes[key]

    async def close_all(self) -> None:
        for lane in self._lanes.values():
            await lane.stop()
        self._lanes.clear()

    @property
    def active_sessions(self) -> list[SessionKey]:
        return list(self._lanes.keys())


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Router:
    def __init__(self, config: Config) -> None:
        self._config             = config
        self._platform_handlers: dict[str, EventHandler] = {}
        self._session_handlers:  dict[SessionKey, EventHandler] = {}
        self._dm_platforms:      dict[SessionKey, str] = {}
        self._session_router     = _SessionRouter(
            config=config,
            event_handler=self._dispatch_event,
            router=self,
        )

    def register_platform_handler(self, platform: str, handler: EventHandler) -> None:
        self._platform_handlers[platform] = handler
        logger.info("Registered platform handler for '%s'", platform)

    def register_reply_handler(self, platform: str, handler: EventHandler) -> None:
        self.register_platform_handler(platform, handler)

    def register_session_handler(self, key: SessionKey, handler: EventHandler) -> None:
        self._session_handlers[key] = handler

    def unregister_session_handler(self, key: SessionKey) -> None:
        self._session_handlers.pop(key, None)

    async def push(self, msg: InboundMessage) -> bool:
        if msg.session_key.chat_type.value == "dm":
            self._dm_platforms[msg.session_key] = msg.author.platform.value
        return await self._session_router.route(msg)

    async def push_synthetic(self, key: SessionKey) -> bool:
        """Queue a generation with no new user message (run(None))."""
        return await self._session_router.enqueue_synthetic(key)

    async def _dispatch_event(self, event: AgentEvent) -> None:
        sk      = event.session_key
        handler = self._session_handlers.get(sk)
        if handler is not None:
            try:
                await handler(event)
            except Exception:
                logger.exception("Session handler failed for %s", event.trace_id)
            return

        platform = sk.platform.value if sk.platform else self._dm_platforms.get(sk)
        if platform is None:
            logger.error("Cannot determine platform for %s — dropping %s", sk, event.trace_id)
            return
        handler = self._platform_handlers.get(platform)
        if handler is None:
            logger.error("No handler for platform '%s' — dropping %s", platform, event.trace_id)
            return
        try:
            await handler(event)
        except Exception:
            logger.exception("Platform handler failed for %s", event.trace_id)

    def reset_session(self, key: SessionKey) -> None:
        lane = self._session_router._lanes.get(key)
        if lane:
            lane.reset()

    def abort_generation(self, key: SessionKey) -> bool:
        lane = self._session_router._lanes.get(key)
        if lane is None:
            return False
        lane.abort()
        return True

    def set_group_activation(self, key: SessionKey, mode: "str | ActivationMode") -> bool:
        if isinstance(mode, str):
            try:
                mode = ActivationMode(mode)
            except ValueError:
                logger.error("set_group_activation: unknown mode %r", mode)
                return False
        lane = self._session_router._lanes.get(key)
        if isinstance(lane, GroupLane):
            lane.set_activation(mode)
            return True
        return False

    def next_session(self, key: SessionKey) -> None:
        """
        Phase 1 stub: next_session is superseded by branch cursor advancement
        in the tree refactor. For now this is a no-op — the old session JSON
        versioning mechanism is removed and Phase 2 will implement proper
        branch forking via ConversationDB.spawn_branch().
        """
        logger.info("next_session(%s) — no-op in Phase 1 tree refactor", key)

    async def shutdown(self) -> None:
        await self._session_router.close_all()

    @property
    def active_sessions(self) -> list[SessionKey]:
        return self._session_router.active_sessions

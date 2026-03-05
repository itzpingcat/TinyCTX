"""
gateway.py — Async session router and lane queues.
No LLM calls, no tools, no memory. Pure routing and async primitives.
Imports only from contracts.py, agent_loop.py, config.py, and stdlib.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from contracts import InboundMessage, OutboundReply, SessionKey, ChatType
from agent_loop import AgentLoop
from config import Config

logger = logging.getLogger(__name__)

ReplyHandler = Callable[[OutboundReply], Awaitable[None]]


# ---------------------------------------------------------------------------
# Lane
# ---------------------------------------------------------------------------

@dataclass
class Lane:
    session_key:   SessionKey
    config:        Config
    reply_handler: ReplyHandler
    loop:          AgentLoop = field(init=False)
    queue:         asyncio.Queue = field(default_factory=asyncio.Queue)
    _worker:       asyncio.Task | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.loop = AgentLoop(session_key=self.session_key, config=self.config)

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain(), name=f"lane:{self.session_key}"
            )

    async def enqueue(self, msg: InboundMessage) -> None:
        await self.queue.put(msg)

    async def _drain(self) -> None:
        while True:
            msg = await self.queue.get()
            try:
                async for chunk in self.loop.run(msg):
                    try:
                        await self.reply_handler(chunk)
                    except Exception:
                        logger.exception("Reply handler raised for chunk %s", chunk.trace_id)
            except Exception:
                logger.exception("AgentLoop raised for %s", self.session_key)
            finally:
                self.queue.task_done()

    def reset(self) -> None:
        """Reset the AgentLoop for this lane — clears conversation context."""
        self.loop.reset()

    async def stop(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Session Router
# ---------------------------------------------------------------------------

class SessionRouter:
    def __init__(self, config: Config, reply_handler: ReplyHandler) -> None:
        self._config        = config
        self._reply_handler = reply_handler
        self._lanes: dict[SessionKey, Lane] = {}

    async def route(self, msg: InboundMessage) -> None:
        lane = self._get_or_create(msg.session_key)
        await lane.enqueue(msg)
        logger.debug("Enqueued %s -> %s", msg.trace_id, msg.session_key)

    def _get_or_create(self, key: SessionKey) -> Lane:
        if key not in self._lanes:
            lane = Lane(session_key=key, config=self._config, reply_handler=self._reply_handler)
            lane.start()
            self._lanes[key] = lane
            logger.info("Opened lane %s", key)
        return self._lanes[key]

    async def close_all(self) -> None:
        for lane in self._lanes.values():
            await lane.stop()
        self._lanes.clear()

    @property
    def active_sessions(self) -> list[SessionKey]:
        return list(self._lanes.keys())


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class Gateway:
    def __init__(self, config: Config) -> None:
        self._config           = config
        self._reply_handlers:  dict[str, ReplyHandler] = {}
        # DM sessions have platform=None on the SessionKey, so we track
        # which platform each DM session arrived from separately.
        # key: SessionKey  value: platform string e.g. "cli", "discord"
        self._dm_platforms:    dict[SessionKey, str] = {}
        self._router           = SessionRouter(
            config=config, reply_handler=self._dispatch_reply
        )

    def register_reply_handler(self, platform: str, handler: ReplyHandler) -> None:
        self._reply_handlers[platform] = handler
        logger.info("Registered reply handler for platform '%s'", platform)

    async def push(self, msg: InboundMessage) -> None:
        # For DM sessions, record which platform this message came from
        # so we can route replies back correctly.
        if msg.session_key.chat_type.value == "dm":
            self._dm_platforms[msg.session_key] = msg.author.platform.value
        await self._router.route(msg)

    async def _dispatch_reply(self, reply: OutboundReply) -> None:
        sk = reply.session_key
        if sk.platform is not None:
            # Group session — platform is on the key
            platform = sk.platform.value
        else:
            # DM session — look up which platform last messaged this session
            platform = self._dm_platforms.get(sk)

        if platform is None:
            logger.error("Cannot determine platform for session %s — dropping %s", sk, reply.trace_id)
            return

        handler = self._reply_handlers.get(platform)
        if handler is None:
            logger.error("No reply handler for platform '%s' — dropping %s", platform, reply.trace_id)
            return

        try:
            await handler(reply)
        except Exception:
            logger.exception("Reply handler failed for %s", reply.trace_id)

    def reset_session(self, key: SessionKey) -> None:
        """Reset a session's context. No-op if session doesn't exist yet."""
        lane = self._router._lanes.get(key)
        if lane:
            lane.reset()

    async def shutdown(self) -> None:
        await self._router.close_all()

    @property
    def active_sessions(self) -> list[SessionKey]:
        return self._router.active_sessions
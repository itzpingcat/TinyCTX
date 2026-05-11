from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from TinyCTX.config import Config
from TinyCTX.contracts import AgentEvent, InboundMessage
from TinyCTX.utils.attachments import save_upload as _save_upload
from TinyCTX.db import ConversationDB
from TinyCTX.utils.commands import CommandRegistry
from TinyCTX.module_registry import ModuleRegistry

logger = logging.getLogger(__name__)

EventHandler = Callable[[AgentEvent], Awaitable[None]]

class Runtime:
    def __init__(self, config: Config) -> None:
        self.config = config
        
        # Shared DB for writing inbound nodes. 
        # AgentCycle will open its own connection for reading/inference.
        workspace = Path(config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self.db = ConversationDB(workspace / "agent.db")

        self.commands = CommandRegistry()
        self.module_registry = ModuleRegistry()

        # SSE / Event Routing
        self._sse_queues: dict[str, list[asyncio.Queue]] = {}
        self._cursor_handlers: dict[str, EventHandler] = {}
        self._platform_handlers: dict[str, EventHandler] = {}
        self._node_platforms: dict[str, str] = {}

        # Concurrency Management
        max_workers = getattr(config, "max_workers", 8)
        self._semaphore = asyncio.Semaphore(max_workers)
        self._active: int = 0
        self._tasks: set[asyncio.Task] = set()
        self._abort_events: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        self.module_registry.load_modules(self)
        logger.info("Runtime started")

    # ------------------------------------------------------------------
    # Entry Point: push()
    # ------------------------------------------------------------------

    async def push(self, msg: InboundMessage, reply_queue: asyncio.Queue | None = None) -> str:
        """
        Accepts InboundMessage, persists to DB, and triggers AgentCycle if needed.
        Always returns the new user node id.
        If reply_queue is provided and msg.trigger is True, events are written into
        it as they arrive. A None sentinel is put when the turn is complete.
        """
        # 1. Track platform for event routing — done after user node is written below.
        # 2. Persist Attachments
        attachment_json = None
        if msg.attachments:
            uploads_dir = Path(self.config.workspace.path).expanduser() / self.config.attachments.uploads_dir
            paths = []
            for att in msg.attachments:
                try:
                    paths.append(str(_save_upload(att, uploads_dir)))
                except Exception:
                    logger.exception("Failed to save attachment %s", att.filename)
            if paths:
                attachment_json = json.dumps(paths, ensure_ascii=False)

        # 3. Write User Node to DB
        state_delta = self._compute_state_delta(msg)
        user_node = self.db.add_node(
            parent_id=msg.tail_node_id,
            role="user",
            content=msg.text,
            author_id=msg.author.user_id,
            author_name=msg.author.username,
            attachment_paths=attachment_json,
            state_delta=json.dumps(state_delta) if state_delta else None,
        )
        
        new_tail_id = user_node.id

        # Track platform under the new user node id so _dispatch_event can route it.
        self._node_platforms[new_tail_id] = msg.author.platform.value

        # 4. Trigger Cycle if requested
        if not msg.trigger:
            return new_tail_id

        # Capacity Check
        if self._active >= (self._semaphore._value + self._active):
            logger.warning("Capacity reached. Node %s persisted but not triggered.", new_tail_id)
            if reply_queue is not None:
                await reply_queue.put(None)
            return new_tail_id

        # Spawn Task
        abort_ev = self._get_abort_event(new_tail_id)
        task = asyncio.create_task(
            self._process(new_tail_id, msg.permission_level, abort_ev, reply_queue),
            name=f"cycle:{new_tail_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return new_tail_id

    # ------------------------------------------------------------------
    # Processing Logic
    # ------------------------------------------------------------------

    async def _process(self, node_id: str, permission_level: int, abort_event: asyncio.Event, reply_queue: asyncio.Queue | None = None) -> None:
        from TinyCTX.agent import AgentCycle
        
        async with self._semaphore:
            self._active += 1
            try:
                agent = AgentCycle(self.config, self.module_registry)
                logger.debug("[runtime] cycle starting for node %s", node_id)
                
                async for event in agent.run(node_id, permission_level, abort_event):
                    await self._dispatch_event(node_id, event)
                    if reply_queue is not None:
                        await reply_queue.put(event)
                
                logger.debug("[runtime] cycle complete for node %s", node_id)
            except Exception:
                logger.exception("Cycle failed for node %s", node_id)
            finally:
                self._active -= 1
                self._abort_events.pop(node_id, None)
                if reply_queue is not None:
                    await reply_queue.put(None)  # sentinel: turn complete

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _compute_state_delta(self, msg: InboundMessage) -> dict:
        prior_state, _ = self.db.load_session_state(msg.tail_node_id)
        delta = {}
        mapping = {
            "platform": msg.author.platform.value,
            "author_id": msg.author.user_id,
            "permission_level": msg.permission_level,
            "server_name": msg.server_name,
            "channel_name": msg.channel_name
        }
        for k, v in mapping.items():
            if v is not None and prior_state.get(k) != v:
                delta[k] = v
        return delta

    async def _dispatch_event(self, node_id: str, event: AgentEvent) -> None:
        # Prefer the event's own tail_node_id for cursor resolution if present,
        # falling back to the original node_id used to spawn the cycle.
        event_node_id = getattr(event, 'tail_node_id', None) or node_id

        # 1. Direct SSE listeners — try event node first, then original
        handler = self._cursor_handlers.get(event_node_id) or self._cursor_handlers.get(node_id)
        if handler:
            await handler(event)
        
        # 2. Platform-wide listeners (e.g. Discord Bridge)
        platform = self._node_platforms.get(node_id)
        for handler in self._platform_handlers.get(platform, []):
            await handler(event)

    def _get_abort_event(self, node_id: str) -> asyncio.Event:
        ev = self._abort_events.get(node_id) or asyncio.Event()
        ev.clear()
        self._abort_events[node_id] = ev
        return ev
    
    def register_platform_handler(self, platform: str, handler: EventHandler) -> None:
        self._platform_handlers.setdefault(platform, []).append(handler)
        
    def abort(self, node_id: str) -> bool:
        if node_id in self._abort_events:
            self._abort_events[node_id].set()
            return True
        return False

    async def shutdown(self) -> None:
        for t in self._tasks: t.cancel()
        if self._tasks: await asyncio.gather(*self._tasks, return_exceptions=True)
        self.db.close()
"""
bridges/discord/turn.py — Agent turn execution for the Discord bridge.

Owns _handle_turn and _typing_keepalive. Imported and called by DiscordBridge.
Separated so bridge.py can focus on routing/access-control, while this module
owns the reply-queue drain loop, streaming indicator logic, and message chunking.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from TinyCTX.contracts import (
    AgentError,
    AgentOutboundFiles,
    AgentThinkingChunk,
    AgentTextChunk,
    AgentTextFinal,
    AgentToolCall,
    AgentToolResult,
    InboundMessage,
)

if TYPE_CHECKING:
    from TinyCTX.bridges.discord.bridge import DiscordBridge

logger = logging.getLogger(__name__)


class ChannelRenderer:
    """
    Renders one AgentEvent stream to a Discord channel — buffering text
    until AgentTextFinal/completion, uploading files, and formatting
    errors. This is the same rendering logic handle_turn() used to run
    inline; it's extracted here so any caller with an event stream and a
    destination channel (a live turn, or a background source like cron via
    Runtime.deliver) gets identical rendering: same chunking, same file
    handling, same error formatting — not a second, drifting reimplementation.

    Usage: feed events one at a time via feed(event), then call flush()
    once the stream is exhausted (None sentinel / turn done) to send any
    buffered text. dehumanize is optional — handle_turn's live path passes
    bridge._dehumanize_mentions; callers without a live bridge (cron) can
    omit it and get raw text.
    """

    def __init__(
        self,
        channel: discord.abc.Messageable,
        max_len: int,
        dehumanize: "Callable[[str], str] | None" = None,
    ) -> None:
        self._channel = channel
        self._max_len = max_len
        self._dehumanize = dehumanize or (lambda t: t)
        self._buf: list[str] = []
        self._suppressed = False

    async def feed(self, event) -> None:
        if isinstance(event, AgentTextChunk):
            self._buf.append(event.text)
        elif isinstance(event, AgentThinkingChunk):
            pass
        elif isinstance(event, AgentTextFinal):
            if event.suppressed:
                self._suppressed = True
                self._buf.clear()
            elif event.text:
                self._buf.append(event.text)
        elif isinstance(event, AgentToolCall):
            logger.debug("Discord: tool call %s", event.tool_name)
        elif isinstance(event, AgentToolResult):
            logger.debug(
                "Discord: tool result %s (%s)",
                event.tool_name, "error" if event.is_error else "ok",
            )
        elif isinstance(event, AgentOutboundFiles):
            for path in event.paths:
                try:
                    await self._channel.send(file=discord.File(path))
                except Exception as exc:
                    logger.warning("Discord: failed to upload file %s: %s", path, exc)
        elif isinstance(event, AgentError):
            await self._channel.send(f"⚠️ {event.message}")
            self._suppressed = True
            self._buf.clear()

    async def flush(self) -> None:
        text = "" if self._suppressed else self._dehumanize("".join(self._buf).strip())
        self._buf.clear()
        if text:
            for i in range(0, len(text), self._max_len):
                await self._channel.send(text[i : i + self._max_len])


def make_platform_handler(bridge: "DiscordBridge") -> "Callable[[str, object], Awaitable[None]]":
    """
    Build the Runtime.register_platform_handler(...) callable for the
    Discord platform: (cursor_key, event) -> None. Cron (or any future
    non-live trigger) delivers one event at a time via this; a
    ChannelRenderer is created lazily per cursor_key and flushed when a
    completion event (AgentTextFinal, AgentError, or the sentinel-shaped
    signal handled by Runtime.deliver's caller) is fed. Since Runtime.deliver
    is called once per event, not once per turn, this handler keeps a
    per-cursor_key buffer alive across calls and flushes on AgentTextFinal
    or AgentError — the two events that end a turn's output.
    """
    renderers: dict[str, ChannelRenderer] = {}

    async def handler(cursor_key: str, event) -> None:
        channel = bridge._active_channels.get(cursor_key)
        if channel is None:
            channel = await bridge._cursor_to_channel(cursor_key)
        if channel is None:
            logger.warning(
                "Discord: platform handler has no channel for cursor_key %r — dropping event", cursor_key
            )
            return

        renderer = renderers.get(cursor_key)
        if renderer is None:
            renderer = ChannelRenderer(channel, bridge._max_len, bridge._dehumanize_mentions)
            renderers[cursor_key] = renderer

        await renderer.feed(event)

        if isinstance(event, (AgentTextFinal, AgentError)):
            await renderer.flush()
            renderers.pop(cursor_key, None)

    return handler


async def typing_keepalive(
    channel: discord.abc.Messageable,
    active_event: asyncio.Event,
    done_event: asyncio.Event,
) -> None:
    """Re-trigger Discord's typing indicator every ~8 s until done_event is set."""
    await active_event.wait()
    while not done_event.is_set():
        try:
            async with channel.typing():
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            await asyncio.sleep(1)


async def handle_turn(
    bridge: "DiscordBridge",
    msg: InboundMessage,
    channel: discord.abc.Messageable,
    cursor_key: str,
) -> None:
    """
    Execute one agent turn. Concurrent Forks (docs/PLAN.md R1): no per-cursor
    lock here anymore — runtime.push() resolves the attach point (Runtime's
    settled_tail) and registers the Run under Runtime's own session lock
    (§6), so overlapping calls for the same cursor_key are safe by
    construction; an overlapping call forks instead of racing.

    Calls runtime.push(), then drains the reply_queue, emitting typing
    indicators and chunked text replies. Advances the cursor mirror (see
    cursors.py docstring) to the final assistant tail node when done.
    After push, records message_id -> new_tail in the cursor store so that
    threads created from this message can fork from the exact right node.
    """
    epoch_at_start = bridge._reset_epoch.get(cursor_key, 0)

    node_id = bridge._get_or_create_cursor(cursor_key)
    msg = dataclasses.replace(msg, tail_node_id=node_id)

    bridge._active_channels[cursor_key] = channel

    done_event   = asyncio.Event()
    typing_ev    = asyncio.Event()
    reply_queue: asyncio.Queue = asyncio.Queue()

    keepalive_task: asyncio.Task | None = None
    if bridge._typing:
        keepalive_task = asyncio.create_task(
            typing_keepalive(channel, typing_ev, done_event)
        )

    new_tail: str | None = None
    try:
        new_tail = await bridge._runtime.push(msg, reply_queue=reply_queue, session_key=cursor_key)
        bridge._advance_cursor(cursor_key, new_tail)
        bridge._node_to_cursor[new_tail] = cursor_key

        # Record message_id -> node so on_thread_create can fork accurately.
        if msg.message_id:
            bridge._store.set_msg_node(msg.message_id, new_tail)

        if not msg.trigger:
            return

        turn_timeout: float | None = (
            float(bridge._opts.get("turn_timeout_s", 0)) or None
        )
        renderer = ChannelRenderer(channel, bridge._max_len, bridge._dehumanize_mentions)

        while True:
            try:
                event = await asyncio.wait_for(
                    reply_queue.get(),
                    timeout=turn_timeout,
                )
            except asyncio.TimeoutError:
                await channel.send("⚠️ Response timed out.")
                break

            if event is None:  # sentinel: turn complete
                break

            # Typing-indicator triggers are turn-local UX, not rendering —
            # kept here rather than in ChannelRenderer, which has no notion
            # of a live typing indicator (cron has no channel.typing()).
            if isinstance(event, AgentTextChunk) and bridge._typing_on_reply:
                typing_ev.set()
            elif isinstance(event, AgentThinkingChunk) and bridge._typing_on_thinking:
                typing_ev.set()
            elif isinstance(event, AgentTextFinal):
                current_epoch = bridge._reset_epoch.get(cursor_key, 0)
                if current_epoch == epoch_at_start and event.tail_node_id:
                    bridge._advance_cursor(cursor_key, event.tail_node_id)
            elif isinstance(event, AgentToolCall) and bridge._typing_on_tools:
                typing_ev.set()

            await renderer.feed(event)

            if isinstance(event, AgentError):
                break

        await renderer.flush()

    except Exception:
        logger.exception("Discord: error handling turn for %s", cursor_key)
    finally:
        done_event.set()
        typing_ev.set()
        bridge._active_channels.pop(cursor_key, None)
        if new_tail:
            bridge._node_to_cursor.pop(new_tail, None)
        if bridge._typing and keepalive_task is not None:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

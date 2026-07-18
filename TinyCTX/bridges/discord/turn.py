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
from typing import TYPE_CHECKING

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
    Execute one agent turn, serialised per cursor_key via bridge._lane_locks.

    Reads the live cursor tail under the lock, calls runtime.push(), then drains
    the reply_queue, emitting typing indicators and chunked text replies.
    Advances the cursor to the final assistant tail node when done.
    After push, records message_id -> new_tail in the cursor store so that
    threads created from this message can fork from the exact right node.
    """
    epoch_at_start = bridge._reset_epoch.get(cursor_key, 0)
    lock = bridge._lane_locks.setdefault(cursor_key, asyncio.Lock())

    async with lock:
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
            new_tail = await bridge._runtime.push(msg, reply_queue=reply_queue)
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
            buf: list[str] = []
            suppressed = False

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

                if isinstance(event, AgentTextChunk):
                    if bridge._typing_on_reply:
                        typing_ev.set()
                    buf.append(event.text)
                elif isinstance(event, AgentThinkingChunk):
                    if bridge._typing_on_thinking:
                        typing_ev.set()
                elif isinstance(event, AgentTextFinal):
                    if event.suppressed:
                        suppressed = True
                        buf.clear()
                    elif event.text:
                        buf.append(event.text)
                    current_epoch = bridge._reset_epoch.get(cursor_key, 0)
                    if current_epoch == epoch_at_start and event.tail_node_id:
                        bridge._advance_cursor(cursor_key, event.tail_node_id)
                elif isinstance(event, AgentToolCall):
                    if bridge._typing_on_tools:
                        typing_ev.set()
                    logger.debug(
                        "Discord: tool call %s for %s", event.tool_name, cursor_key
                    )
                elif isinstance(event, AgentToolResult):
                    logger.debug(
                        "Discord: tool result %s (%s) for %s",
                        event.tool_name,
                        "error" if event.is_error else "ok",
                        cursor_key,
                    )
                elif isinstance(event, AgentOutboundFiles):
                    for path in event.paths:
                        try:
                            await channel.send(file=discord.File(path))
                        except Exception as exc:
                            logger.warning(
                                "Discord: failed to upload file %s: %s", path, exc
                            )
                elif isinstance(event, AgentError):
                    await channel.send(f"⚠️ {event.message}")
                    break

            # Send accumulated text (unless the agent replied NO_REPLY).
            text = "" if suppressed else bridge._dehumanize_mentions("".join(buf).strip())
            if text:
                for i in range(0, len(text), bridge._max_len):
                    await channel.send(text[i : i + bridge._max_len])

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


async def run_turn_loop(
    bridge: "DiscordBridge",
    msg: InboundMessage,
    channel: discord.abc.Messageable,
    cursor_key: str,
) -> None:
    """
    Drive handle_turn() for cursor_key, then drain any messages that were
    buffered (in bridge._pending) while the agent was generating.

    If messages arrived while busy, every buffered message except the last
    is recorded as passive context (bridge._push_passive), and the last one
    becomes the trigger for another handle_turn() pass — so a burst of
    messages sent while the agent is replying gets answered together in one
    follow-up turn, rather than one turn per message.

    bridge._generating[cursor_key] stays True for the whole loop so that
    _dispatch_turn() keeps buffering incoming messages until we're done.
    """
    try:
        while True:
            await handle_turn(bridge, msg, channel, cursor_key)

            pending = bridge._pending.pop(cursor_key, None)
            if not pending:
                break

            *rest, msg = pending
            for passive in rest:
                await bridge._push_passive(passive, cursor_key)
    finally:
        bridge._generating[cursor_key] = False

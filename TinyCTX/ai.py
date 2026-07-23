"""
ai.py — Async OpenAI-compatible LLM and Embedder clients.
Streams SSE, assembles tool call deltas, yields typed events.
Imports only aiohttp and stdlib. No internal project imports.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Any
import aiohttp
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)
from TinyCTX.config import ModelConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority queue — process-wide admission control for outbound LLM/embedding
# requests. All callers go through LLM.stream() / Embedder.embed() /
# Embedder.embed_one(), passing an optional `priority` (lower runs first,
# ties are FIFO). The queue itself is module-level state, not an object
# passed around — configure_parallel() is the only external touchpoint.
# ---------------------------------------------------------------------------

_queue_heap: list = []          # heap of _QueueItem
_queue_seq = itertools.count()
_queue_lock_cond: asyncio.Condition | None = None   # created lazily, needs a running loop
_queue_workers: list[asyncio.Task] = []
_queue_parallel = 3              # overwritten by configure_parallel()


def configure_parallel(n: int) -> None:
    """
    Set the number of concurrent in-flight LLM/embedding requests. Called
    once at startup after Config is loaded (config.yaml's `parallel:` key).
    Safe to call before the first request — workers spin up lazily.
    """
    global _queue_parallel
    _queue_parallel = max(1, n)


@dataclass(order=True)
class _QueueItem:
    priority:  int
    seq:       int
    is_stream: bool                    = field(compare=False, default=False)
    fut:       "asyncio.Future | None" = field(compare=False, default=None)
    coro_fn:   Any                     = field(compare=False, default=None)
    gen_fn:    Any                     = field(compare=False, default=None)
    out_queue: "asyncio.Queue | None"  = field(compare=False, default=None)


_STREAM_DONE = object()  # sentinel — distinguishes "generator finished" from any real event


async def _ensure_workers() -> None:
    """Lazily spin up worker tasks on first use. No caller needs to know this exists."""
    global _queue_lock_cond
    if _queue_lock_cond is None:
        _queue_lock_cond = asyncio.Condition()
    while len(_queue_workers) < _queue_parallel:
        _queue_workers.append(asyncio.create_task(_queue_worker(len(_queue_workers))))


async def _queue_worker(worker_id: int) -> None:
    """
    Pops the highest-priority item and runs it to completion before looking
    at the heap again — this is what makes `parallel` a real concurrency
    cap. For a one-shot call (embeddings), "running it" means awaiting a
    coroutine. For a stream, "running it" means draining the real async
    generator and forwarding each item live as it's produced — nothing is
    buffered or replayed, the worker is just busy for the generator's
    entire lifetime.
    """
    while True:
        async with _queue_lock_cond:
            while not _queue_heap:
                await _queue_lock_cond.wait()
            item = heapq.heappop(_queue_heap)
        try:
            if item.is_stream:
                async for event in item.gen_fn():
                    await item.out_queue.put(event)
                await item.out_queue.put(_STREAM_DONE)
            else:
                result = await item.coro_fn()
                if not item.fut.done():
                    item.fut.set_result(result)
        except Exception as e:
            logger.exception("[ai] queue worker %d request failed", worker_id)
            if item.is_stream:
                await item.out_queue.put(e)
            elif not item.fut.done():
                item.fut.set_exception(e)


async def _enqueue(priority: int, coro_fn) -> Any:
    """One-shot call (embeddings). Submit a zero-arg coroutine factory, await its result."""
    await _ensure_workers()
    fut = asyncio.get_event_loop().create_future()
    item = _QueueItem(priority=priority, seq=next(_queue_seq), is_stream=False, fut=fut, coro_fn=coro_fn)
    async with _queue_lock_cond:
        heapq.heappush(_queue_heap, item)
        _queue_lock_cond.notify()
    return await fut


async def _enqueue_stream(priority: int, gen_fn) -> AsyncIterator[Any]:
    """
    Streaming call. `gen_fn` is a zero-arg callable returning an async
    generator (e.g. `lambda: self._stream_with_retry(messages, tools)`).
    The generator is NOT started until a worker admits this item — that's
    the entire mechanism for "don't emit anything while queued." Once
    admitted, the worker drains it and forwards each yielded item through
    `out_queue` immediately, so the caller sees live events, not a replay.
    """
    await _ensure_workers()
    out_queue: asyncio.Queue = asyncio.Queue()
    item = _QueueItem(priority=priority, seq=next(_queue_seq), is_stream=True, gen_fn=gen_fn, out_queue=out_queue)
    async with _queue_lock_cond:
        heapq.heappush(_queue_heap, item)
        _queue_lock_cond.notify()

    while True:
        event = await out_queue.get()
        if event is _STREAM_DONE:
            return
        if isinstance(event, Exception):
            raise event
        yield event


# ---------------------------------------------------------------------------
# Yield types
# ---------------------------------------------------------------------------

@dataclass
class TextDelta:
    text: str

@dataclass
class ThinkingDelta:
    text: str

@dataclass
class ToolCallAssembled:
    """Emitted once per tool call, after all argument chunks are assembled."""
    call_id:   str
    tool_name: str
    args:      dict[str, Any]

@dataclass
class LLMError:
    message: str


LLMEvent = TextDelta | ThinkingDelta | ToolCallAssembled | LLMError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inject_cache_control(messages: list[dict]) -> list[dict]:
    """
    Return a shallow copy of messages with Anthropic prompt-caching headers
    injected on the last system message.

    The last system message's content is converted to a content-block list
    if it isn't already one, and a cache_control block is appended:
        {"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}

    If no system message is present, messages are returned unchanged.
    """
    # Find the last system message index
    last_sys = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "system"),
        None,
    )
    if last_sys is None:
        return messages

    messages = list(messages)  # shallow copy — don't mutate caller's list
    msg = dict(messages[last_sys])  # copy the message dict
    content = msg.get("content", "")

    if isinstance(content, str):
        # Convert plain string to a content-block list with cache_control
        msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        # Already a list — tag the last block
        blocks = list(content)
        last_block = dict(blocks[-1])
        last_block["cache_control"] = {"type": "ephemeral"}
        blocks[-1] = last_block
        msg["content"] = blocks

    messages[last_sys] = msg
    return messages


# ---------------------------------------------------------------------------
# Chat client
# ---------------------------------------------------------------------------

class LLM:
    """
    Async OpenAI-compatible streaming client.
    Works with Anthropic (via OpenAI-compat endpoint), OpenAI, OpenRouter,
    LM Studio, Ollama, or any base_url that speaks /v1/chat/completions.
    """

    def __init__(
        self,
        base_url:         str,
        api_key:          str,
        model:            str,
        max_tokens:       int        = 2048,
        temperature:      float      = 0.7,
        timeout:          int        = 60,
        budget_tokens:    int | None = None,
        reasoning_effort: str | None = None,
        cache_prompts:    bool       = False,
    ) -> None:
        self.model            = model
        self.endpoint         = f"{base_url.rstrip('/')}/chat/completions"
        self.api_key          = api_key
        self.max_tokens       = max_tokens
        self.temperature      = temperature
        self.timeout          = aiohttp.ClientTimeout(total=None, sock_read=timeout)
        self.budget_tokens    = budget_tokens
        self.reasoning_effort = reasoning_effort
        self.cache_prompts    = cache_prompts

    async def stream(
        self,
        messages: list[dict],
        tools:    list[dict] | None = None,
        priority: int = 10,
    ) -> AsyncIterator[LLMEvent]:
        """
        Stream a completion. Yields TextDelta, ToolCallAssembled, or LLMError.
        Retries on transient connection errors (up to 3 attempts, exponential backoff).
        Tool call argument chunks are assembled before yielding — callers
        always receive complete, parseable args dicts.

        `priority` controls admission order when multiple requests are in
        flight at once (lower runs first, ties are FIFO). A queued request
        emits nothing until a worker admits it — once admitted, it streams
        live exactly as before, with no buffering or replay.
        """
        try:
            async for event in _enqueue_stream(priority, lambda: self._stream_with_retry(messages, tools)):
                yield event
        except aiohttp.ClientConnectionError as e:
            yield LLMError(f"Connection failed after retries: {e}")

    @retry(
        retry=retry_if_exception_type(aiohttp.ClientConnectionError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=False,
    )
    async def _stream_with_retry(
        self,
        messages: list[dict],
        tools:    list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        # Expand image tool results: a tool turn whose content is a list
        # containing an image_url block is not valid as-is (OpenAI-compat APIs
        # don't support image content in tool messages). Split it into a plain
        # text tool result + a synthetic user turn with the image_url block.
        expanded: list[dict] = []
        for msg in messages:
            if (
                msg.get("role") == "tool"
                and isinstance(msg.get("content"), list)
                and any(b.get("type") == "image_url" for b in msg["content"])
            ):
                image_blocks = [b for b in msg["content"] if b.get("type") == "image_url"]
                text_blocks  = [b for b in msg["content"] if b.get("type") != "image_url"]
                text_content = text_blocks[0]["text"] if text_blocks else ""
                expanded.append({**msg, "content": text_content})
                expanded.append({"role": "user", "content": image_blocks})
            else:
                expanded.append(msg)
        messages = expanded

        # --- cache_prompts: inject ephemeral cache_control on last system message ---
        if self.cache_prompts:
            messages = _inject_cache_control(messages)

        # --- budget_tokens: Anthropic extended thinking ---
        temperature = self.temperature
        if self.budget_tokens is not None:
            if temperature != 1.0:
                logger.warning(
                    "budget_tokens requires temperature=1; overriding %.2f → 1.0 for model %s",
                    temperature, self.model,
                )
                temperature = 1.0

        payload: dict[str, Any] = {
            "model":       self.model,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  self.max_tokens,
            "stream":      True,
        }
        if tools:
            payload["tools"] = tools
        if self.budget_tokens is not None:
            payload["thinking"] = {"type": "enabled", "budget_tokens": self.budget_tokens}
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        # llama.cpp specific flag
        if self.cache_prompts:
            payload["cache_prompt"] = True

        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # Accumulate tool call fragments keyed by index
        # { index: {"id": str, "name": str, "args_buf": str} }
        tool_buf: dict[int, dict] = {}

        # Compact message summary — one line per message, no content dumps
        def _image_fmt(block: dict) -> str:
            """Extract image format from an image_url block for logging."""
            url = ""
            img = block.get("image_url", {})
            if isinstance(img, dict):
                url = img.get("url", "")
            elif isinstance(img, str):
                url = img
            if url.startswith("data:"):
                # data:image/png;base64,... → png
                try:
                    mime = url[5:url.index(";")]
                    return f"image_url({mime})"
                except ValueError:
                    return "image_url(data:?)"
            # plain URL — grab extension
            ext = url.rsplit(".", 1)[-1].split("?")[0][:8] if "." in url else "?"
            return f"image_url({ext})"

        def _msg_summary(m):
            c = m.get("content", "")
            if isinstance(c, list):
                parts = "/".join(
                    (_image_fmt(b) if isinstance(b, dict) and b.get("type") == "image_url" else b.get("type", "?"))
                    if isinstance(b, dict) else "?"
                    for b in c
                )
                detail = f"[{parts}]"
            else:
                detail = f"{len(str(c))}ch"
            tc = f" +{len(m['tool_calls'])}tc" if m.get("tool_calls") else ""
            return f"{m.get('role','?')}: {detail}{tc}"
        logger.debug("[ai] POST %s | %d msgs: %s", self.endpoint, len(payload["messages"]), " | ".join(_msg_summary(m) for m in payload["messages"]))

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    self.endpoint, headers=headers, json=payload
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield LLMError(f"HTTP {resp.status}: {body}")
                        return

                    async for raw in resp.content:
                        line = raw.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choices = data.get("choices")
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})

                        # Reasoning/thinking tokens (DeepSeek-R1 / llama-swap style)
                        if reasoning := delta.get("reasoning_content"):
                            yield ThinkingDelta(text=reasoning)

                        # Text content
                        if text := delta.get("content"):
                            yield TextDelta(text=text)

                        # Tool call fragments — assemble before yielding
                        for tc in delta.get("tool_calls", []):
                            idx = tc.get("index", 0)
                            if idx not in tool_buf:
                                tool_buf[idx] = {"id": "", "name": "", "args_buf": ""}
                            buf = tool_buf[idx]
                            if tc.get("id"):
                                buf["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                buf["name"] = fn["name"]
                            buf["args_buf"] += fn.get("arguments", "")

                    # Stream done — emit assembled tool calls
                    for buf in tool_buf.values():
                        try:
                            args = json.loads(buf["args_buf"] or "{}")
                        except json.JSONDecodeError:
                            args = {"_raw": buf["args_buf"]}
                        yield ToolCallAssembled(
                            call_id=buf["id"],
                            tool_name=buf["name"],
                            args=args,
                        )

        except aiohttp.ClientConnectionError:
            raise  # tenacity will retry on this
        except Exception as e:
            yield LLMError(str(e))


# ---------------------------------------------------------------------------
# Embedding client
# ---------------------------------------------------------------------------

class Embedder:
    """
    Async OpenAI-compatible embedding client.
    Calls /v1/embeddings and returns float vectors.

    Works with any server that speaks the OpenAI embeddings API:
      - OpenAI          base_url = https://api.openai.com/v1
      - llama-swap      base_url = http://localhost:8085/v1
      - Ollama          base_url = http://localhost:11434/v1
      - LM Studio       base_url = http://localhost:1234/v1

    Usage:
        embedder = Embedder.from_config(agent.config.get_embedding_model("embed"))
        vectors = await embedder.embed(["hello", "world"])
    """

    def __init__(
        self,
        base_url:   str,
        api_key:    str,
        model:      str,
        batch_size: int = 32,
        timeout:    int = 60,
    ) -> None:
        self.model      = model
        self.endpoint   = f"{base_url.rstrip('/')}/embeddings"
        self.api_key    = api_key
        self.batch_size = batch_size
        self.timeout    = aiohttp.ClientTimeout(total=timeout)

    @classmethod
    def from_config(cls, cfg: "ModelConfig", batch_size: int = 32, timeout: int = 60) -> "Embedder":  # noqa: F821
        """Build an Embedder from a ModelConfig with kind='embedding'."""
        api_key = cfg.api_key  # resolves from env or returns "" for N/A
        return cls(
            base_url=cfg.base_url,
            api_key=api_key,
            model=cfg.model,
            batch_size=batch_size,
            timeout=timeout,
        )

    async def embed(self, texts: list[str], priority: int = 10) -> list[list[float]]:
        """
        Embed a list of strings. Returns one float vector per input text,
        in the same order as the input. Batches automatically.

        `priority` controls admission order when multiple requests are in
        flight at once (lower runs first, ties are FIFO).

        Raises RuntimeError on API error.
        """
        if not texts:
            return []

        async def _run():
            results: list[list[float]] = []
            for i in range(0, len(texts), self.batch_size):
                batch = texts[i : i + self.batch_size]
                results.extend(await self._call(batch))
            return results

        return await _enqueue(priority, _run)

    async def embed_one(self, text: str, priority: int = 10) -> list[float]:
        """Convenience wrapper — embed a single string."""
        vecs = await self.embed([text], priority=priority)
        return vecs[0]

    async def _call(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": texts}
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    self.endpoint, headers=headers, json=payload
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"Embedding API HTTP {resp.status}: {body}")
                    data = await resp.json()
        except aiohttp.ClientConnectionError as e:
            raise RuntimeError(f"Embedding API connection failed: {e}") from e

        # Sort by index to guarantee order matches input regardless of server behaviour
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]

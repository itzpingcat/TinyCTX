# PLAN: LLM Request Priority Queue

**Feature:** All outbound LLM/embedding requests get an integer `priority`.
Requests run FIFO *within* a priority tier, lowest number first. A
`parallel` setting caps how many requests run at once (default 3; Kamie's
`config.yaml` sets it to 1 — single `llama-swap` backend, no point sending
concurrent requests it can't actually run in parallel).

**Key constraint (per Kamie):** this stays entirely inside `ai.py`. No new
file, no `RequestQueue` object passed around through `runtime.py` →
`agent.py` → modules. `LLM.stream()` and `Embedder.embed()`/`embed_one()`
keep their exact current signatures plus one new optional `priority: int`
kwarg. Every call site elsewhere in the codebase changes by adding
`priority=N` to an existing call — nothing else about those files changes.

---

## Core Design

- **One module-level queue, owned by `ai.py` itself.** Not instantiated by
  callers, not constructed in `runtime.py`, not passed into `AgentCycle` or
  `LibrarianRunner`. It's process-global state inside `ai.py`, the same way
  you'd reach for a module-level `logger`.
- A `heapq`-backed structure keyed on `(priority, seq)` — `seq` is a
  monotonic counter assigned at submit time so same-priority requests stay
  FIFO (heapq isn't stable on `priority` alone).
- A pool of `parallel` asyncio worker tasks, lazily started on first use
  (no explicit `.start()` call needed anywhere — see "Lazy startup" below,
  this is what keeps other files untouched).
- `LLM.stream(messages, tools=None, priority=10)` and
  `Embedder.embed(texts, priority=10)` / `embed_one(text, priority=10)`
  internally submit to the shared queue and await/iterate the result.
  Every other line of `ai.py` — `_stream_with_retry`, the retry decorator,
  `_inject_cache_control`, the SSE parsing loop, `Embedder._call` — is
  unchanged. The queue wraps the *existing* methods; it doesn't touch their
  internals.
- A worker holds its slot for the whole duration of a streaming request
  (submit → last chunk), not just the initial POST — same as before.
- **Streaming stays live, not buffered.** A request sitting *in* the queue
  emits nothing — there's nothing to emit yet, because its generator hasn't
  started. The moment a worker picks it up, it runs the real
  `_stream_with_retry()` generator and forwards each event to the caller
  as it arrives, exactly like today. "Queued" and "buffered" aren't the
  same thing: the wait happens before generation starts, not by collecting
  output and replaying it. Once admitted, a request streams token-by-token
  same as it always has — `parallel: 1` just means only one stream is
  *running* at a time, not that streams render delayed.

---

## `ai.py` Changes

Everything below is additive to the existing file. Nothing in the current
`LLM` / `Embedder` bodies changes except the first line of `stream()` /
`embed()` / `embed_one()`, which now goes through `_enqueue(...)` instead of
running directly.

```python
# --- new imports, top of file ---
import heapq
import itertools

# --- new module-level state, near `logger = logging.getLogger(__name__)` ---

_queue_heap: list = []          # heap of _QueueItem
_queue_seq = itertools.count()
_queue_lock_cond: asyncio.Condition | None = None   # created lazily, needs a running loop
_queue_workers: list = []
_queue_parallel = 3             # overwritten by configure_parallel()


def configure_parallel(n: int) -> None:
    """
    Set the number of concurrent in-flight requests. Called once at startup
    from Config (TinyCTX/config/__main__.py reads `parallel:` from
    config.yaml). Safe to call before or after the first request — if
    workers are already running, extra/fewer workers are reconciled on the
    next request.
    """
    global _queue_parallel
    _queue_parallel = max(1, n)


@dataclass(order=True)
class _QueueItem:
    priority:  int
    seq:       int
    is_stream: bool                           = field(compare=False)
    fut:       "asyncio.Future | None"        = field(default=None, compare=False)
    coro_fn:   Any                            = field(default=None, compare=False)
    gen_fn:    Any                            = field(default=None, compare=False)
    out_queue: "asyncio.Queue | None"         = field(default=None, compare=False)


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
    coroutine. For a stream, "running it" means draining an async generator
    and forwarding each item live — the worker is busy for the generator's
    entire lifetime, but nothing about its output is delayed or replayed.
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
    item = _QueueItem(priority=priority, seq=next(_queue_seq), fut=fut, coro_fn=coro_fn,
                       is_stream=False, gen_fn=None, out_queue=None)
    async with _queue_lock_cond:
        heapq.heappush(_queue_heap, item)
        _queue_lock_cond.notify()
    return await fut


_STREAM_DONE = object()  # sentinel — distinguishes "generator finished" from "item is None"


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
    item = _QueueItem(priority=priority, seq=next(_queue_seq), fut=None, coro_fn=None,
                       is_stream=True, gen_fn=gen_fn, out_queue=out_queue)
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
```

### `LLM.stream()` — one-line change at the top

```python
async def stream(
    self,
    messages: list[dict],
    tools:    list[dict] | None = None,
    priority: int = 10,
) -> AsyncIterator[LLMEvent]:
    """
    Stream a completion. Yields TextDelta, ToolCallAssembled, or LLMError.
    Retries on transient connection errors (up to 3 attempts, exponential backoff).
    `priority` controls queue ordering when multiple requests are in flight —
    lower runs first, ties are FIFO. Queued requests emit nothing until
    admitted; once a worker picks this up, it streams live exactly as
    before — no buffering or replay.
    """
    try:
        async for event in _enqueue_stream(priority, lambda: self._stream_with_retry(messages, tools)):
            yield event
    except aiohttp.ClientConnectionError as e:
        yield LLMError(f"Connection failed after retries: {e}")
```

This replaces the current body. `_stream_with_retry` itself is completely
untouched — `_enqueue_stream` just delays *when* it starts running, then
forwards what it yields as it yields it. No list, no collect-then-replay,
no buffering step at all. The only added latency is queue wait time, same
as it would be for any admission-controlled system; once running, behavior
is identical to today.

### `Embedder.embed()` / `embed_one()` — same pattern

```python
async def embed(self, texts: list[str], priority: int = 10) -> list[list[float]]:
    """
    Embed a list of strings. Returns one float vector per input text,
    in the same order as the input. Batches automatically.
    `priority` controls queue ordering — lower runs first, ties are FIFO.
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
```

`_call()` itself — the actual HTTP POST to `/embeddings` — is untouched.

Embeddings stay one-shot request/response (no generator to forward), so
`_enqueue()` is sufficient there — only `LLM.stream()` needed the live-
forwarding `_enqueue_stream()` path above.

---

## Call Sites Changed (every one is a 1-line diff: add `priority=N`)

| File | Line (current) | Change |
|---|---|---|
| `agent.py` `_stream_inference()` | `async for ev in llm.stream(messages, tools=tools):` | `async for ev in llm.stream(messages, tools=tools, priority=0):` |
| `modules/memory/librarian_agents.py` `_agent_loop()` | `async for event in llm.stream(messages, tools=tool_defs):` | `async for event in llm.stream(messages, tools=tool_defs, priority=15):` |
| `modules/memory/dedup_agents.py` | `async for event in llm.stream([...], tools=None):` | `async for event in llm.stream([...], tools=None, priority=15):` |
| `modules/memory/tools.py` (`_embed_query`, ~line 354) | `await _embedder.embed_one(text)` (or similar) | `await _embedder.embed_one(text, priority=5)` |
| `modules/rag/databanks.py` (`q_vec = await embedder...`) | direct `.embed(...)` call | add `priority=5` |
| `modules/rag/indexer.py` (`embeddings = await ...`) | direct `.embed(...)` call, batch indexing | add `priority=20` |

No signature changes, no new params, no new imports needed in any of these
files. They already hold an `llm`/`embedder` instance; they just pass one
more kwarg into a method they're already calling.

### `modules/memory/__main__.py` — `_YieldingLLM` is now redundant

This file is the one place that needs more than a one-liner, because it
already has a hand-rolled version of "make background LLM calls wait their
turn" (`_YieldingLLM`, busy-waiting on `self._is_busy()`). With priority
ordering built into `ai.py` itself, this wrapper's job is now done by
passing `priority=15` at the librarian's call site instead.

- Recommend deleting `_YieldingLLM` (~lines 85–102) and constructing
  `LibrarianRunner`'s LLM directly: `self._llm = llm` instead of
  `self._llm = _YieldingLLM(llm, self._user_cycles_active)`.
- This is a recommendation, not a requirement of this plan — if Kamie wants
  to keep `_YieldingLLM` as a belt-and-suspenders layer on top of queue
  priority, that's fine too, it just becomes redundant rather than load-
  bearing. Flagging because `_user_cycles_active()` may be used elsewhere;
  worth a quick grep before deleting it outright.

---

## `config/__main__.py` Changes

```python
@dataclass
class Config:
    ...
    max_tool_cycles: int = 20
    parallel:        int = 3     # max concurrent LLM/embedding requests in flight
    ...
```

```python
# in load():
cfg = Config(
    ...
    max_tool_cycles=int(raw.get("max_tool_cycles", 20)),
    parallel=int(raw.get("parallel", 3)),
    ...
)
```

```python
_KNOWN_KEYS = {
    "models", "llm", "router", "bridges", "gateway", "workspace",
    "logging", "max_tool_cycles", "parallel", "context", "attachments", "permissions",
}
```

Validation: raise `ValueError` if `parallel < 1` (same style as the
`budget_tokens > 0` / `tokens_per_image > 0` guards already in
`_parse_model`).

`config.yaml` gets:

```yaml
parallel: 1
```

### Wiring `parallel` into `ai.py` without passing objects around

One call, once, at startup — wherever `main.py` / `runtime.py` currently
calls `config.load(...)`:

```python
from TinyCTX.ai import configure_parallel
cfg = config.load(args.config)
configure_parallel(cfg.parallel)
```

That's the only place `runtime.py`/`main.py` touches this feature. No
`RequestQueue` instance is constructed or threaded through `AgentCycle`,
`LibrarianRunner`, or any module's `register_runtime`/`register_agent`.

---

## Priority Conventions

Plain `int`, lower = more urgent, not enforced as an enum:

| Priority | Caller |
|---|---|
| 0 | Interactive user-facing turn (`agent.py` main loop) |
| 5 | Query-time embeddings that block a user turn (`rag/databanks.py`, `memory/tools.py`) |
| 10 | Default (unspecified `priority=`) |
| 15 | Librarian / dedup background agent loops |
| 20 | RAG indexer batch embedding (background, nobody waiting) |

---

## Files Changed

| File | Change |
|---|---|
| `TinyCTX/ai.py` | module-level queue state + `configure_parallel()`; `LLM.stream()` routes through `_enqueue_stream()` (live event forwarding, nothing emitted while queued); `Embedder.embed()`/`embed_one()` gain `priority: int = 10` and route through `_enqueue()`. All other methods (`_stream_with_retry`, `_call`, `_inject_cache_control`) untouched. |
| `TinyCTX/config/__main__.py` | `Config.parallel: int = 3`; parse + validate in `load()`; add to `_KNOWN_KEYS` |
| `main.py` (or wherever config is loaded at startup) | one call to `configure_parallel(cfg.parallel)` after `config.load()` |
| `TinyCTX/agent.py` | `llm.stream(..., priority=0)` — one kwarg added at the existing call site |
| `TinyCTX/modules/memory/librarian_agents.py` | `llm.stream(..., priority=15)` |
| `TinyCTX/modules/memory/dedup_agents.py` | `llm.stream(..., priority=15)` |
| `TinyCTX/modules/memory/tools.py` | `embed_one(..., priority=5)` |
| `TinyCTX/modules/rag/databanks.py` | `embed(..., priority=5)` |
| `TinyCTX/modules/rag/indexer.py` | `embed(..., priority=20)` |
| `TinyCTX/modules/memory/__main__.py` | (recommended, not required) delete `_YieldingLLM`, construct `LibrarianRunner`'s LLM directly |
| `config.yaml` | add `parallel: 1` |

No changes to `runtime.py`, `module_registry.py`, `contracts.py`, `db.py`,
`context.py`, bridges, or the gateway HTTP layer — none of them need to
know the queue exists.

---

## Why Streaming Doesn't Need a Tradeoff

The earlier draft of this plan assumed "goes through a queue" implied
"buffer the whole response, then replay it." It doesn't: the queue only
controls *when a generator is allowed to start*. `_enqueue_stream()` holds
the request until a worker is free, then runs `_stream_with_retry()` and
forwards each event into `out_queue` the instant it's produced —
identical token-by-token behavior to today, just admission-gated. A
queued-but-not-yet-running request has nothing to forward because nothing
has been generated yet, which is exactly the "don't emit anything while
queued" behavior wanted, with no replay step and no extra latency once a
request is admitted.

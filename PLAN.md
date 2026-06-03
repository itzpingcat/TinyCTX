# PLAN: Interleaved Interruptions (v4)

**Feature:** While an AgentCycle is mid-run, new user messages are buffered in
a per-cycle queue and injected into the running cycle at safe checkpoints —
without aborting, without spawning a new cycle, without new dataclasses, without
the bridge knowing anything about cycle internals.

---

## Core Design

- `Runtime` holds `_interrupt_queues: dict[str, asyncio.Queue]` keyed by the
  **start node_id** of each active cycle.
- `runtime.push()` is the only entry point for all messages. It checks whether
  an active cycle is an ancestor of the incoming `msg.tail_node_id`. If yes,
  it routes the message into that cycle's interrupt queue instead of spawning
  a new cycle. The DB write is skipped — the cycle owns all DB writes for
  interrupt messages.
- The cycle calls `_drain_interrupts(node_id, queue) -> str` at any safe
  checkpoint. It writes nodes to the DB itself, chained off whatever `node_id`
  it passes in, and returns the new tip node_id.
- At end of cycle, `runtime.close_interrupt_queue(node_id)` atomically removes
  the queue from the dict and returns it for one final drain. After this point
  `push()` can no longer route into the queue.

---

## `_drain_interrupts` — the central primitive

```python
def _drain_interrupts(self, node_id: str, queue: asyncio.Queue) -> str:
    """
    Drain all pending interrupt messages from queue, writing each as a user
    node chained off node_id. Returns the new tip node_id (unchanged if queue
    was empty).
    """
    while not queue.empty():
        content_str, author = queue.get_nowait()
        node = self.db.add_node(
            parent_id=node_id,
            role="user",
            content=content_str,
            author_id=author.username,
            author_name=None,
        )
        node_id = node.id
    return node_id
```

Every call site follows the same pattern:

```python
node_id = self._drain_interrupts(node_id, interrupt_queue)
self.context.set_tail(node_id)
```

---

## Drain Points in `agent.py`

### 1. Top of outer loop (between tool batches / before each LLM call)

```python
for cycle_num in range(max_cycles):
    if abort_event.is_set(): ...

    if interrupt_queue:
        node_id = self._drain_interrupts(node_id, interrupt_queue)
        self.context.set_tail(node_id)

    await self.context.run_async_hooks(...)
    messages, _ = self.context.assemble()
    ...
```

### 2. Inside tool loop, before each tool result write

```python
for tc in tool_calls_list:
    yield AgentToolCall(...)

    if interrupt_queue:
        node_id = self._drain_interrupts(node_id, interrupt_queue)
        self.context.set_tail(node_id)

    result = await self._execute_tool(tc)
    self.context.add_tool_result(result)
    node_id = self.context.tail_node_id
    ...
```

### 3. No-tool-calls exit check

```python
if not tool_calls_list:
    if interrupt_queue:
        node_id = self._drain_interrupts(node_id, interrupt_queue)
        self.context.set_tail(node_id)
        if node_id != prev_node_id:
            continue  # interrupts arrived — loop again so LLM sees them
    final_text = response_text
    break
```

### 4. End of cycle — final drain after closing the queue

```python
# After the loop exits, before AgentTextFinal:
if interrupt_queue is not None:
    final_queue = runtime.close_interrupt_queue(start_node_id)
    if final_queue:
        node_id = self._drain_interrupts(node_id, final_queue)
        self.context.set_tail(node_id)
        if node_id != pre_close_node_id:
            # Interrupts arrived in the closing window — re-enter loop.
            # Reset loop state and continue. The queue is now closed so
            # push() cannot route any more messages here; this is the
            # last possible drain.
            # Implementation: use a goto-equivalent by wrapping the loop
            # in a while True and jumping back to top, or restructure
            # agent.run() to use a while loop instead of for.
            continue  # only works if loop is `while` not `for`
```

**Note on loop structure:** The outer loop should be `while cycle_num < max_cycles`
rather than `for cycle_num in range(max_cycles)` so that drain point 4 can
`continue` back to the top cleanly.

---

## `runtime.py` Changes

### Queue lifecycle in `_process()`

```python
async def _process(self, node_id, permission_level, abort_event, reply_queue=None):
    from TinyCTX.agent import AgentCycle

    interrupt_queue: asyncio.Queue = asyncio.Queue()
    self._interrupt_queues[node_id] = interrupt_queue

    async with self._semaphore:
        self._active += 1
        try:
            agent = AgentCycle(self.config, self.module_registry)
            async for event in agent.run(
                node_id, permission_level, abort_event,
                interrupt_queue=interrupt_queue,
                runtime=self,
                start_node_id=node_id,
            ):
                if reply_queue is not None:
                    await reply_queue.put(event)
        except Exception:
            logger.exception("Cycle failed for node %s", node_id)
        finally:
            self._active -= 1
            # Queue may already be gone if cycle called close_interrupt_queue.
            # Pop defensively in case cycle errored before closing it.
            self._interrupt_queues.pop(node_id, None)
            self._abort_events.pop(node_id, None)
            if reply_queue is not None:
                await reply_queue.put(None)
```

### Routing in `push()`

```python
async def push(self, msg: InboundMessage, reply_queue=None) -> str:
    # Resolve content string (attachment processing)
    content_str = self._resolve_content(msg)

    if msg.trigger:
        # Check if an active cycle covers this tail
        for start_node_id, queue in self._interrupt_queues.items():
            if self.db.is_ancestor(start_node_id, msg.tail_node_id):
                # Route into running cycle — skip DB write, cycle owns it
                queue.put_nowait((content_str, msg.author))
                return msg.tail_node_id

    # Normal path — write node and optionally spawn cycle
    user_node = self.db.add_node(
        parent_id=msg.tail_node_id,
        role="user",
        content=content_str,
        ...
    )
    new_tail_id = user_node.id

    if not msg.trigger:
        return new_tail_id

    abort_ev = self._get_abort_event(new_tail_id)
    task = asyncio.create_task(
        self._process(new_tail_id, msg.author.permission_level, abort_ev, reply_queue),
        name=f"cycle:{new_tail_id}"
    )
    ...
    return new_tail_id
```

### `close_interrupt_queue()`

```python
def close_interrupt_queue(self, node_id: str) -> asyncio.Queue | None:
    """
    Atomically remove and return the interrupt queue for this cycle.
    After this call, push() will no longer route messages into this queue.
    The caller should do one final _drain_interrupts() on the returned queue.
    """
    return self._interrupt_queues.pop(node_id, None)
```

### `_resolve_content()` — extracted from push()

Attachment processing and content string resolution, extracted so it can be
called before the routing decision:

```python
def _resolve_content(self, msg: InboundMessage) -> str:
    workspace = Path(self.config.workspace.path).expanduser().resolve()
    primary_name = self.config.llm.primary
    model_cfg = self.config.models.get(primary_name)
    effective_text = f"[Replying to {msg.reply_to_author}]\n{msg.text}" if msg.reply_to_author else msg.text
    content = _build_content_blocks(
        text=effective_text,
        attachments=msg.attachments,
        model_cfg=model_cfg,
        att_cfg=self.config.attachments,
        workspace=workspace,
    ) if msg.attachments else effective_text
    return json.dumps(content, ensure_ascii=False) if isinstance(content, list) else content
```

---

## `db.py` — one new method

```python
def is_ancestor(self, ancestor_id: str, descendant_id: str) -> bool:
    """
    Returns True if ancestor_id is an ancestor of descendant_id.
    Walks the parent chain of descendant_id upward.
    """
    cur = descendant_id
    while cur:
        if cur == ancestor_id:
            return True
        row = self._conn.execute(
            "SELECT parent_id FROM nodes WHERE id = ?", (cur,)
        ).fetchone()
        cur = row[0] if row else None
    return False
```

Called by `push()` once per active cycle per incoming message.
Active cycles bounded by `max_workers` (default 8). Ancestor walk is at most
a few hops from cursor tip to branch root. Acceptable cost.

---

## The Streaming Race — Solved

Interrupt arrives while LLM is mid-stream:

- `push()` routes into the interrupt queue — no DB write.
- LLM finishes. `context.add()` writes assistant node off current tail. Tail
  advances to assistant node.
- Drain point 1 fires at top of next loop iteration. `_drain_interrupts` writes
  interrupt node off assistant node. Chain is linear.

No fork. The cycle owns all DB writes for interrupt messages, and only writes
them at safe checkpoints after the assistant node exists.

---

## The End-of-Cycle Race — Solved

Interrupt arrives as cycle is finishing:

- `close_interrupt_queue()` atomically pops the queue from `_interrupt_queues`.
  From this point, `push()` cannot route new messages here.
- `_drain_interrupts` is called on the returned queue. In a single-threaded
  asyncio event loop, nothing can put into the queue between `pop` and `drain`
  because there is no `await` between them — both are synchronous. The final
  drain is guaranteed complete.
- If anything was drained, the outer loop continues one more time (last
  possible iteration — queue is closed). LLM sees the interrupt messages.
- If nothing was drained, `AgentTextFinal` is yielded and the turn ends.
- `_process` `finally` calls `_interrupt_queues.pop(node_id, None)` — no-op
  since cycle already closed it. Safe.

---

## What the Queue Holds

`tuple[str, User]` — `(resolved_content_str, author)`. Not a new dataclass.
Content is fully resolved (attachments processed) by `_resolve_content()` in
`push()` before routing. The cycle just writes it straight to the DB.

---

## Files Changed

| File | Change |
|------|---------|
| `TinyCTX/db.py` | `is_ancestor(ancestor_id, descendant_id) -> bool` |
| `TinyCTX/runtime.py` | `_interrupt_queues`; routing in `push()`; `close_interrupt_queue()`; `_resolve_content()` extracted; queue lifecycle in `_process()` |
| `TinyCTX/agent.py` | `interrupt_queue`, `runtime`, `start_node_id` params to `run()`; `_drain_interrupts()` helper; four drain points; outer loop as `while` |
| `example.config.yaml` | `experimental.interleaved_interruptions: bool` |

No changes to `contracts.py`, `context.py`, `bridges/`, any module, CLI bridge,
gateway. The bridge calls `push()` exactly as today.

# PLAN: Concurrent Forks

**Status:** design of record. Supersedes the root-level `PLAN.md`
("Interleaved Interruptions v4"), which is deleted — see
[What This Replaces](#what-this-replaces).

**Feature:** many concurrent `AgentCycle`s per conversation, peer-to-peer, with
no coordinator agent and no subagent handoff. One agent running concurrent
versions of itself.

---

## 1. The Problem

The cap of one live cycle per conversation is not a DB limitation. The node
tree branches natively, and two branches off a common parent already have
fully isolated contexts. The cap exists because **coherence and isolation are
the same knob**, and nothing turns them independently:

- Turn isolation up (separate branches) and forks diverge. A never learns B
  existed, so A's final message asserts a world state that B already changed,
  and whichever branch the next user message lands on is missing the other's
  history. The model forgets it did things.
- Turn isolation down (merge branches) and you get bleed — the Python fork
  reads the image-gen fork's tool calls — plus context bloat linear in every
  fork's transcript.

The resolution: **isolate reasoning, share effects.** Divergent reasoning
across forks is fine and desirable. A divergent world-model is the bug. So
forks exchange a bounded digest of what each other *did*, never a transcript
of what each other *thought*.

### Scope

Cycles are ephemeral asyncio objects and stay that way. Nothing about a
*running* cycle is persisted. The data cycles operate on is already in the
node tree; the only thing missing is a live map of what is running and where.
That map is in-memory and dies with the process, which is correct — after a
restart there are no runs, and abandoned branches are just unreferenced
history.

### What Each Fork Needs To Know

Exactly two things:

1. **Which cycles are currently running**, rendered as a footer block in the
   style of `equipment_manifest_footer`.
2. **When a fork finishes, its final output** — user-visible text only, no
   tool calls, no thinking traces.

A fork never sees entries it authored itself; those are already in its own
transcript, and duplicating them invites double-counting.

---

## 2. The Lifetime Split

These two requirements look alike and are not. They differ in lifetime, and
that difference dictates two different mechanisms.

| | Roster of running cycles | Finished fork's output |
|---|---|---|
| Truth | true *now*, false in 30s | happened once, true forever |
| Must | regenerate every assemble; vanish when the cycle ends | survive re-assembly and trimming |
| Mechanism | prompt provider (ephemeral) | node written into the branch (durable) |
| Core changes | none | one primitive |

Building the second one as a prompt provider is the trap: the model sees the
completion once, the footer re-renders without it, and it's gone. That is the
same lobotomy on a delay.

---

## 3. Core Model

### 3.1 `Run` — in-memory, per live cycle

```python
@dataclass
class Run:
    id:            str
    session_key:   str                  # bridge cursor key
    intent:        str                  # triggering user message, truncated
    root_node_id:  str
    status:        str                  # running | done | failed | aborted
    started_at:    float
    inbox:         asyncio.Queue        # Exogenous events, see 3.3
    cycle:         AgentCycle | None    # for live head reads
```

Held in `Runtime._runs: dict[str, Run]`. Not persisted. `run.id` is **not** a
node id — this split is what makes two cycles on one conversation
representable at all. Today a node id doubles as message identity, cycle
identity (`_abort_events[node_id]`, task name `cycle:{node_id}`), append
point, and context-window definition, and there is nowhere to put the second
cycle.

`intent` is the user message that triggered the run, truncated. Agent-declared
intent is a later refinement, not needed for v1.

### 3.2 `settled_tail` — one per session

The single node new inbound messages attach to. Owned by Runtime, not by a
bridge. Replaces the scalar `cursor_key -> node_id` entry in
`bridges/discord/cursors.py::CursorStore`.

**One attach rule, both behaviours, no mode detection:**

> New messages always attach to `settled_tail`.

- Nothing running → `settled_tail` is a quiet leaf → attaching is linear
  continuation.
- A run is in flight → `settled_tail` has not advanced past that run's root →
  attaching there *is* a fork.

Forking is not a mode triggered by a concurrency threshold. It is what
"attach to `settled_tail`" does when `settled_tail` has not advanced yet.

### 3.3 `Exogenous` — the inbox entry type

```python
@dataclass
class Exogenous:
    kind:    str    # "fork_finished" | "nudge" | ...
    role:    str    # role to write the node as
    content: str    # pre-rendered
```

One inbox per run, carrying typed exogenous events, drained at safe points,
each rendering to a node. **Naming the category rather than the feature is the
point.** A queue per event source — one for interrupts, one for fork
completions, one for cron, one for MCP notifications — is the failure mode this
design exists to avoid. Adding a source here is a new `kind`, not a new queue.

---

## 4. Rules

### R1 — Attach

New inbound message attaches to `session.settled_tail`. (See 3.2.)

### R2 — Completion

When a run finishes:

```python
def finish(run):
    notice = render_digest(run)          # intent + final text; no tools, no thinking
    settled_tail[run.session_key] = run.head_node_id
    for peer in running_peers(run.session_key, exclude=run):
        peer.inbox.put(Exogenous("fork_finished", role=…, content=notice))
```

Two halves:

- **`settled_tail` advances to the last finisher**, not the first. See §5 for
  why this is complete.
- **Fan-out** delivers the digest to every peer still running. This is not a
  patch on the tree; it is what keeps the §5 invariant true.

### R3 — Drain

A running cycle drains its inbox at two points, writing each entry as a node
off its current head:

1. **Top of the outer loop**, before `assemble()` — so the next LLM call sees
   it.
2. **Once before finalizing** — the case where a peer finished while this run
   was streaming its final message. This is the re-prompt / confirm-last-message
   point: if the drain wrote anything, loop once more so the model can rewrite
   or confirm what it was about to say.

No mid-tool-loop drain. A fork digest is not latency-sensitive the way a user
interrupt is.

---

## 5. The Completeness Invariant

> For any two runs in a session, either one is an ancestor of the other, or
> they overlapped in time.

- **Non-overlapping.** The later run started after the earlier finished, so it
  branched off a `settled_tail` that had already advanced to the earlier run's
  head. It inherits the earlier run by ancestry. No mechanism needed.
- **Overlapping.** The earlier finisher fanned its digest into the later run's
  inbox (R2), and the later run drained it into its own branch (R3).

Either way the later finisher's head transitively contains the earlier run.
Inducting over that:

> **The last run to finish always has everything.**

Which is why `settled_tail = last finisher's head` is complete by
construction, and why no grafting, merging, or reconciliation pass is needed.

**Worked example.** A, B, C all start at t=0; finish at t=1, t=2, t=3.

```
t=1  A finishes → settled_tail = A.head; fan-out to B, C
t=2  B finishes → settled_tail = B.head; fan-out to C
t=3  C finishes → settled_tail = C.head; no peers

C.head chain = C-full + digest(A) + digest(B)
```

Nothing is lost. A and B's full transcripts stay addressable on their own
branches; a future turn that needs more than the digest can HEAD-jump to them.
Nothing is ever force-merged, and nothing is canonical by default.

---

## 6. The Race

The one correctness-critical window. X finishes at the same moment Y starts:

- Y resolves `settled_tail` *before* X advanced it → Y forks off the stale
  tail, so it does not inherit X.
- X fans out *before* Y is in `_runs` → Y is not a peer, so it does not receive
  the digest.

Y misses X entirely, and since Y is the last finisher it becomes the trunk —
the loss is permanent.

**Fix.** One lock per session, held across both critical sections:

- **Start:** resolve `settled_tail` and register the run in `_runs` under the
  lock.
- **Finish:** advance `settled_tail` and fan out under the same lock.

Then a starting run either observes the advanced tail or is visible to the
fan-out. Never neither.

This is the only serialization the design requires. In particular it is *not*
a lock on cycle execution — runs execute freely and concurrently; only the
start and finish transitions are serialized.

---

## 7. Concrete Changes

### `Runtime` (`runtime.py`)

- `_runs: dict[str, Run]`, `_settled: dict[str, str]` (session_key → node_id),
  `_session_locks: dict[str, asyncio.Lock]`.
- `start_run(session_key, …) -> Run` — under the session lock: resolve
  `settled_tail`, create the `Run`, register it.
- `finish_run(run, final_text)` — under the session lock: advance
  `settled_tail`, fan out digests (R2).
- `runs_in_session(session_key)` — for the roster provider and fan-out.
- `push()` attaches to `settled_tail` and spawns a run rather than taking a
  node id from the bridge.
- Fix the capacity check at the top of `push()`: it reads the private
  `self._semaphore._value` and is evaluated before acquiring, so it is racy.

### `AgentCycle` (`agent.py`)

- Accept a `Run` handle; keep `run.cycle` pointed at self so the roster can read
  a live head.
- `_drain_inbox(node_id) -> node_id` — same shape as the old
  `_drain_interrupts`, but role and content come from the `Exogenous` entry
  instead of being hardcoded.
- Two drain points (R3). The outer loop becomes `while` rather than
  `for … in range(…)` so the pre-finalize drain can loop once more.
- Call `runtime.finish_run(...)` on completion.

### `modules/forks/` (new module — requirement 1 in full)

- `register_runtime(runtime)` — the pattern `modules/subagents` already uses.
- `register_agent(agent)` registers a prompt provider:

  ```python
  agent.context.register_prompt("running_forks", provider,
                                role=ROLE_USER, priority=…)
  ```

  The provider reads `_runtime.runs_in_session(...)`, filters out self and
  anything not running, and renders one line per run. `register_prompt` already
  re-invokes the provider on every `assemble()`, so freshness is automatic.
  `role=ROLE_USER` places it in the deferred-footer position outside the cached
  prefix — same reason `equipment_manifest_footer` exists, and correct here
  because the roster changes every turn.

**No changes to `context.py` or `db.py`.** Requirement 1 lands entirely on
existing extension points.

### `bridges/discord/`

- Delete `CursorStore`'s cursor map and `_advance_cursor`; the runtime owns the
  tail. The `discord_msg_nodes.json` map (message_id → node_id, used for thread
  forking) stays — that is genuine bridge bookkeeping.
- Delete `_lane_locks`, `_generating`, `_pending`, and the buffering in
  `_dispatch_turn`. The bridge calls `push()` per message and drains the reply
  queue. This is where the one-fork cap actually lives — `Runtime._semaphore` is
  already 8.

### `modules/subagents/`

Subsumed. `spawn_agent` becomes "start a run that does not take `settled_tail`",
`wait_agent` becomes a lookup in `_runs`. The `SubagentTask` registry,
`max_concurrent`, and the TTL pruning all go.

---

## 8. What This Replaces

The old root `PLAN.md` gave each cycle a queue for exactly one event source
(user interrupts), reconstructed cycle/branch relationships at runtime via
`db.is_ancestor()` walks, and needed four drain points plus a whole section on
the end-of-cycle close-queue race. It added a dict, a queue, a method, four
drain points, and a config flag, and deleted nothing.

Two things change here.

**Interrupts stop being a feature.** Under R1, a new user message while a cycle
runs forks off `settled_tail` — the running cycle is not interrupted, it is
raced, and its digest reaches the new fork via R2. No interrupt queue, no
`is_ancestor()` routing, no `close_interrupt_queue()`.

**The end-of-cycle race stops mattering.** The old plan needed an atomic
close-then-drain because a lost interrupt was unrecoverable. Here a digest that
arrives after a run has finished still reaches future turns through
`settled_tail`. There is a durable fallback path, so the window is not
correctness-critical.

Drain points: four → two. Queues: one per cycle per event source → one inbox
per run. Net, this deletes more than it adds.

**Known cost:** "no wait, do X instead" now burns the running fork's tokens
instead of redirecting it. `runtime.abort()` already exists, so the agent or the
user can kill it — but it is a real behaviour change, not a free win.

---

## 9. Inter-Fork Messaging (Nudges)

A fork can send an advisory message to a peer — "the user changed their mind,
stop generating and revert the file you wrote." There is deliberately **no hard
abort**, so forks cannot kill each other.

This costs almost no new architecture. It is the case `Exogenous.kind` exists
for (§3.3): a new kind, a new tool, and nothing else.

```python
nudge_fork(run_id: str, message: str)          # agent-facing tool
  → runtime.nudge(target_id, from_run, text)
  → target.inbox.put(Exogenous("nudge", role=…, content=…))
```

The roster (§7, `modules/forks/`) already exposes run ids and intents, so
addressing needs no new scheme. The target drains it at the normal R3 points.

### 9.1 Rules

**Advisory by construction.** A nudge arrives as text in the target's context.
The target reads it and decides. There is no kill path, so there is nothing to
reach for by accident.

**One-way. No acks.** The sender learns nothing directly; the only feedback
channel is the target's eventual completion digest (R2). This is a hard rule,
not a simplification — request/response between forks is how a coordinator
sneaks back in. Given an ack, the model will build "spawn three forks, poll each
for status, aggregate", which is `wait_agent` reappearing under a new name.
One-way plus advisory prevents that structurally.

**Same session only.** Constrained to peers sharing a `session_key`. A
cross-session nudge is just a different conversation.

**Free-text, not a verb enum.** One `kind` ("nudge") carrying `from_run` plus
free text. Semantics live in the message; enumerating stop/pause/revert verbs
buys nothing the model cannot express.

**Nudging a finished run** returns a "that fork already completed" tool result.
No lock needed — the benign failure is a `put` into an inbox nobody drains
again.

### 9.2 Rendering Carries Authority

This is what actually enforces "no murdering each other." If a peer's nudge
renders like a user message, the target obeys it like one. The rendered node
must mark the sender as subordinate to the user:

```
[nudge from fork 4c1f — "handle the user's CSV request"]
User changed their mind on the image. Stop generating, don't post it.

(Advisory, from a peer fork. Not from the user. You decide whether to comply.)
```

### 9.3 Revert Is Mostly Already Free

The target is on its own branch, so its own tool calls are already in its own
context. It can read what it did and undo it with no new machinery. The only
gap is context trimming having evicted early tool calls on a long run.

Do **not** add a per-run effect log for this. That would be a second digest
system running alongside the completion digest, projecting the same data
differently. Live with the trimming gap until it demonstrably bites.

### 9.4 Latency

Drain point 1 (R3) fires between tool batches, so a nudge lands before the
target's next LLM call. Worst case the target wastes one batch of tool calls.

If that proves too slow, the knob is a third drain point inside the tool loop —
added **once, for all kinds**, never specifically for nudges. Drain points are a
property of the cycle, not of the event kind. A drain point per feature is the
same failure mode as a queue per feature (§3.3).

### 9.5 Interaction With §5

None. Nudges only flow between overlapping runs and never need to reach the
trunk: whether the target complied is visible either way in its completion
digest. The completeness invariant is unaffected.

---

## 10. Open Decisions

1. **Role of an injected digest node.** Assistant-role is literally true (same
   agent) and the existing adjacent-merge in `context.py` handles
   assistant-after-assistant; it reads naturally on the trunk for future turns.
   User-role with an explicit `[fork 3 finished] …` wrapper is safer with
   OpenAI-compat backends and makes it unambiguous to a *peer* that this
   happened elsewhere rather than something it said. Likely different answers
   for the trunk versus the peer inject. Decide by reading real transcripts.

2. **Module state assuming one live cycle.** `agent._subagent_tasks`,
   `modules/todo`'s per-session list, and module-global `_runtime` references
   all predate concurrency. Audit before enabling N runs — `todo` especially,
   since "session" there probably means something scalar.

3. **Digest length policy.** Currently "final output, no tools, no thinking."
   Unbounded if a fork's final message is long. May need truncation, and if so
   it should share one `render(run, level)` function with any future
   full-transcript recall so the two do not diverge.

4. **Multiple concurrent streams into one Discord channel.** Sending is
   milliseconds and each stream is its own message, so this is cosmetic rather
   than architectural — but two forks replying at once may still read oddly.
   Revisit after observing it.

5. **Nudge ping-pong.** A nudges B, B nudges A back, unbounded. Probably
   self-limiting, since each fork sees its own prior nudges in its own
   transcript — but that is a bet on the model, and a weaker one may not hold.
   A per-pair cap is the obvious guard and also the obvious thing to regret
   building before observing the failure. Watch for it; do not pre-empt it.

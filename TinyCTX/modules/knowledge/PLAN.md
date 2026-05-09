# Knowledge Module Refactor Plan

## Goal

Ditch `libbuffer/` flat files entirely. Track librarian progress via a generic
`flags` column on `nodes` in `agent.db`. All agent.db writes go through `db.py`.

---

## Schema Changes

Add one column to the `nodes` table in `ConversationDB`:

- `flags TEXT` — JSON array of strings, default `'[]'`. Example: `["librarian_visited", "memory_visited"]`

No module gets its own column. Flags are just strings — present in the list means flagged, absent means not.

---

## db.py — Flag Utilities

All agent.db writes (including flag mutations) go through `db.py`. New helpers:

```python
def add_flag(node_id: str, flag: str) -> None: ...
def remove_flag(node_id: str, flag: str) -> None: ...
def has_flag(node_id: str, flag: str) -> bool: ...
def get_flags(node_id: str) -> list[str]: ...
def get_nodes_without_flag(flag: str) -> list[Node]: ...  # for poll queries

def flag_branch(node_id: str, flag: str) -> list[str]:
    """
    Walk parent chain from node_id upward, adding flag to each node,
    stopping at (and excluding) the first ancestor that already has the flag.
    Returns the list of node_ids that were flagged.
    """
    ...
```

No raw SQL for flag logic anywhere outside `db.py`.

---

## Librarian Poll Cycle — New Logic

### 1. Find tail nodes
Query via `db.py`: all current tail nodes (nodes with no children).

### 2. Walk and flag each tail
For each tail node, call `db.flag_branch(tail_id, "librarian_visited")`.
This walks up to the first already-visited ancestor, flags everything in between,
and returns the node ids that were just flagged — those become the batch for
the librarian agent.

Skip tails that are already flagged (branch returns empty).

### 3. Dispatch agents (under asyncio.Lock)
The flag_branch + dispatch step is wrapped in an `asyncio.Lock` so two concurrent
poll cycles don't process overlapping branches.

Turn each batch into `[username]: message` text, spawn a librarian agent per batch.
Agents are pure stateless workers — LLM calls and graph write tool calls only.
No agent.db access in agents.

### 4. Track tasks
```python
active_tasks: set[asyncio.Task]
```

Tasks are fire-and-forget from a tracking perspective — nodes are already flagged
before dispatch, so no reap-and-mark step is needed.

---

## Retry / Crash Safety

Nodes are flagged `"librarian_visited"` before the agent runs, not after.
This means a crash mid-agent does not cause a retry — the branch is already marked.

If retry-on-failure is needed in the future, a `"librarian_pending"` flag can be
introduced: flag pending before dispatch, promote to visited on success, clear on
failure. Not needed for the initial implementation.

---

## Files Changed

| File | Change |
|------|--------|
| `ConversationDB` | Add `flags` column + migration |
| `db.py` | Add flag utility functions including `flag_branch` |
| `librarian_process.py` | Replace buffer file polling with flag-based walk logic, all writes via `db.py` |
| `buffer.py` | **Delete** |
| `__init__.py` | Remove `libbuffer_dir` from default config |
| `__main__.py` | Remove `libbuffer_dir` wiring |

---

## Collision Safety Summary

| Scenario | Safe? | Reason |
|----------|-------|--------|
| Main agent writing nodes vs librarian reading | Yes | SQLite WAL |
| Main agent writing nodes vs librarian marking flags | Yes | Different rows, WAL |
| Two librarian poll cycles overlapping walks | Yes | asyncio.Lock around flag_branch+dispatch |
| Two agents processing overlapping branches | Yes | Branch flagged before dispatch |
| Librarian crash mid-agent | Accepted | Nodes pre-flagged; no retry by design |
| Agent writes flags directly | N/A | Agents never touch agent.db |

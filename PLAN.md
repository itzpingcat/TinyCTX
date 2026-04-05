# TinyCTX — Production CLI Refactor Plan

## Goal

Turn TinyCTX into a proper installable command-line tool:

```
tinyctx onboard          # setup wizard
tinyctx start            # start gateway daemon (no CLI bridge)
tinyctx stop             # stop daemon
tinyctx status           # show daemon health
tinyctx launch cli       # attach interactive CLI to running daemon
```

The daemon runs headlessly (gateway + all non-CLI bridges). The CLI bridge
is attached on demand as a foreground client that talks to the daemon over
HTTP. Multiple `launch` clients can attach and detach without stopping the
daemon.

---

## Package layout (new files only)

```
                              ← new top-level CLI package
  __init__.py
  __main__.py                     ← entry: python -m cmd / tinyctx
  commands/
    __init__.py
    start.py                      ← spawn daemon, write pid file, poll health
    stop.py                       ← kill daemon, clean pid file
    status.py                     ← print health from /v1/health
    onboard.py                    ← thin wrapper: calls onboard.__main__.main()
    launch.py                     ← dispatch `tinyctx launch <target>`
  pid.py                          ← pid file helpers (read/write/check/clean)

pyproject.toml                    ← console_scripts entry point
```

No new bridge directories. The existing `bridges/cli/` is rewritten in-place.

---

## Changes to existing files

### `contracts.py`

Add one module-level constant:

```python
# Sentinel checked by main.py — bridges that set this are skipped on auto-start.
MANUAL_LAUNCH_ATTR = "MANUAL_LAUNCH"
```

### `bridges/cli/__main__.py`  ← rewritten

Add at module level:

```python
MANUAL_LAUNCH = True   # do not auto-start via main.py
```

The `run(gateway)` entry point (called by main.py's bridge loader) becomes a
no-op since main.py skips MANUAL_LAUNCH bridges before calling it.

The new `run_detached(gateway_url, api_key, options)` function is the real
entry point, called by `commands/launch.py`. It connects to the running
daemon over HTTP and runs the interactive TUI in the foreground.

All rendering code (`CLITheme`, `CLIBridge`, `handle_event`, Rich Live display,
clipboard helpers, `/help`, `/copy`, `/paste`) stays exactly as-is. Only the
message-send and event-receive backend changes.

### `main.py`

In the bridge-loading loop, after importing the bridge module, add one check:

```python
if getattr(mod, "MANUAL_LAUNCH", False):
    logger.debug("Bridge '%s' is manual-launch only — skipping auto-start", name)
    continue
```

That is the only change to `main.py`.

### `gateway/__main__.py`  ← rewritten

See full section below. The session-cursor model is replaced with a
node_id-based API, and the internal event dispatch is fixed to support
concurrent lanes correctly.

### `config.yaml` / `example.config.yaml`

No structural changes.

---

## What the CLI bridge actually does in-process (current)

Tracing `bridges/cli/__main__.py` against `router.py` and `db.py`:

| Operation | In-process call |
|---|---|
| Startup: load/create cursor | Read `cursors/cli` file → `db.get_node(id)` or `db.add_node(root, "system", "session:cli")` |
| Startup: open lane + load modules | `router.open_lane(node_id, "cli")` |
| Send message | `router.push(InboundMessage(tail_node_id=cursor, ...))` |
| Receive events | Platform handler registered via `router.register_platform_handler("cli", fn)` |
| Wait for turn end | `await self._reply_done.wait()` — set by `AgentTextFinal` or `AgentError` |
| After turn: advance cursor | Read `lane.loop._tail_node_id` → write to `cursors/cli` |
| `/reset`: new branch | `db.add_node(root, "system", "session:cli")` → write cursor file → `router.reset_lane(old)` → `router.open_lane(new, "cli")` |
| Slash commands | `router.commands.dispatch(text, ctx)` |

---

## The multi-lane dispatch problem

The existing `router._cursor_handlers` dict maps `node_id → single handler`.
`register_cursor_handler` is last-writer-wins. This was fine when the gateway
was the only HTTP client (one session at a time), but breaks with concurrent
lanes because:

1. Two simultaneous `/message` requests on different node_ids each call
   `register_cursor_handler(node_id, _sse)`. Each registration is correct
   for its own node_id — this case actually works fine.

2. Two simultaneous `/message` requests on the **same** node_id (e.g. two
   clients both attached to the "cli" cursor) would race: the second
   `register_cursor_handler` overwrites the first, so the first client gets
   no events.

3. After a turn completes, `unregister_cursor_handler` removes the entry.
   If the platform handler fallback is the only remaining handler for that
   lane's platform (e.g. Discord), events from that lane still route correctly
   via `_platform_handlers`. But if no platform handler is registered for
   `Platform.API` (the gateway never registers one), events from lanes that
   have had their cursor handler removed are silently dropped.

The fix is entirely inside `gateway/__main__.py` — `router.py` is not touched.

**Solution: the gateway maintains its own per-lane fanout table.**

The gateway registers **one persistent cursor handler per active node_id** the
first time any SSE stream opens for that node. That handler writes to a
`node_id → set[asyncio.Queue]` fanout table. Each SSE response gets its own
queue. When a stream disconnects its queue is removed; when the last queue for
a node_id is removed the cursor handler is unregistered.

```
router._cursor_handlers[node_id] = _fanout_handler(node_id)
                                          ↓ puts event into
gateway._fanout[node_id] = { queue_A, queue_B, ... }
                                          ↓ each queue read by
                             SSE response coroutine A, B, ...
```

This means:
- Multiple SSE clients on the same node_id all receive all events.
- Cursor handler lifetime is tied to whether any client is listening, not to
  the request lifecycle.
- The router's single-handler-per-node_id constraint is fully respected.
- No changes to `router.py`.

---

## New gateway API  (`gateway/__main__.py` rewrite)

Four endpoints total. The session-string / cursor-map / `gateway.json` model
is gone.

### Endpoints

```
POST   /v1/lane/open       open (or no-op) a lane; bootstrap cursor if needed
POST   /v1/lane/message    push a message, receive SSE reply
POST   /v1/lane/branch     create a new branch node, return its node_id
DELETE /v1/lane/abort      abort in-flight generation for a node_id
GET    /v1/health          always public
```

All endpoints except `/v1/health` require `Authorization: Bearer <api_key>`.

---

### `POST /v1/lane/open`

Mirrors `router.open_lane(node_id, "api")` + cursor bootstrap.

**Request:**
```json
{ "node_id": "<uuid or null>" }
```

- If `node_id` is null/absent: create a new branch off DB root, return its id.
- If `node_id` is provided and exists in `agent.db`: call
  `router.open_lane(node_id, "api")`, return it as-is.
- If `node_id` is provided but does not exist in `agent.db`: treat as null
  (create new branch off root).

**Response:**
```json
{ "node_id": "<uuid>" }
```

---

### `POST /v1/lane/message`

Mirrors `router.push(InboundMessage(...))` with SSE fanout.

**Request:**
```json
{
  "node_id": "<uuid>",
  "text": "hello",
  "attachments": [{ "name": "f.png", "data_b64": "...", "mime_type": "image/png" }]
}
```

`text` or `attachments` (or both) required. Returns 429 if queue full.

Internally:
1. Parse body, build `InboundMessage(tail_node_id=node_id, author=CLI_AUTHOR, ...)`.
2. Ensure fanout entry exists for `node_id` (register cursor handler if first
   subscriber).
3. Create a fresh `asyncio.Queue` for this request, add to fanout set.
4. Call `await router.push(msg)`.
5. Open SSE stream, drain queue until `{"type": "done"}` event arrives.
6. Remove queue from fanout set; if set is now empty, unregister cursor handler.

**Response:** SSE stream:

```
data: {"type": "thinking_chunk", "text": "..."}
data: {"type": "text_chunk",     "text": "..."}
data: {"type": "text_final",     "text": "..."}
data: {"type": "tool_call",      "tool_name": "...", "call_id": "...", "args": {...}}
data: {"type": "tool_result",    "tool_name": "...", "call_id": "...", "output": "...", "is_error": false}
data: {"type": "error",          "message": "..."}
data: {"type": "done",           "node_id": "<new tail uuid>"}
```

The `done` event carries the **new tail node_id** after the turn completes,
derived from `event.tail_node_id` on the `AgentTextFinal` or `AgentError`
event. The client uses this to advance its local cursor.

---

### `POST /v1/lane/branch`

Creates a new child node in `agent.db`. Branching is non-destructive — no
existing lane is reset or modified.

**Request:**
```json
{
  "parent_node_id": "<uuid or null>"
}
```

- `parent_node_id`: the node to branch off. If null/absent, branches off the
  DB root. Can be any valid node_id — the root, an arbitrary mid-conversation
  node, or the current tail.

Server-side: `db.add_node(parent_node_id or root.id, role="system", content="session:branch")`.

**Response:**
```json
{ "node_id": "<new uuid>" }
```

Client follows up with `POST /v1/lane/open` for the new node_id to warm the
lane before sending messages.

**CLI `/reset` usage:**
```json
{ "parent_node_id": null }
```
Branches off root (clean slate). The client simply updates its local cursor
to the new node_id and opens the new lane — the old lane is left intact in
the DB and its in-memory context will expire naturally when unused.

**Branch from mid-conversation:**
```json
{ "parent_node_id": "<some earlier node_id>" }
```
Creates a new branch from an arbitrary point in the tree without touching any
existing lane.

---

### `DELETE /v1/lane/abort`

**Request body:**
```json
{ "node_id": "<uuid>" }
```

Calls `router.abort_generation(node_id)`. Returns 204.

---

### `GET /v1/health`

Always public. Returns status, uptime, per-lane summary (node_id, turn count,
queue depth).

---

### Internal fanout table structure

```python
# Inside _make_app / app state:
app["fanout"]: dict[str, set[asyncio.Queue]] = {}

async def _fanout_handler_for(node_id: str, app) -> callable:
    async def _handler(event) -> None:
        payload = _event_to_dict(event)  # includes "done" with node_id
        for q in list(app["fanout"].get(node_id, [])):
            await q.put(payload)
    return _handler

# On first subscriber for node_id:
app["fanout"][node_id] = set()
router.register_cursor_handler(node_id, await _fanout_handler_for(node_id, app))

# On each SSE request:
q = asyncio.Queue()
app["fanout"][node_id].add(q)
# ... stream from q ...
app["fanout"][node_id].discard(q)
if not app["fanout"][node_id]:
    router.unregister_cursor_handler(node_id)
    del app["fanout"][node_id]
```

---

### What is removed from the old gateway

All of the following are deleted:

- `GET /v1/sessions` — session list
- `DELETE /v1/sessions/{id}` — session reset
- `POST /v1/sessions/{id}/message`
- `PUT /v1/sessions/{id}/generation`
- `DELETE /v1/sessions/{id}/generation`
- `POST /v1/sessions/{id}/reset`
- `GET /v1/sessions/{id}/history`
- `_load_cursor_map`, `_save_cursor_map`, `_resolve_node_id`
- `workspace/cursors/gateway.json` is no longer created or read

### What is kept

- `GET /v1/workspace/files/{path}`
- `PUT /v1/workspace/files/{path}`

---

## `bridges/cli/__main__.py` — rewrite detail

### Kept exactly as-is

- `CLITheme` dataclass and all color/text defaults
- All render methods: `handle_event`, `_start_reply`, `_get_live_render`,
  `_stop_live`, `_ensure_live`
- `_preprocess` (code block label injection)
- Clipboard helpers: `_read_clipboard_text`, `_write_clipboard_text`
- `/copy`, `/paste`, `/help` built-in slash commands
- `_prompt` (async stdin reader)
- `CLIBridge.__init__`, `_console`, `_live`, `_theme`, `_reply_done`
- `_load_cli_cursor` / `_persist_cli_cursor` — client still tracks its own
  cursor in `~/.tinyctx/cursors/cli` for resume across restarts

### What changes

`CLIBridge` gains two attributes set by `run_detached`:

```python
self._gateway_url: str
self._api_key: str
```

`self._cursor` remains a node_id UUID — same as before, just managed locally.

**`CLIBridge._http_headers()`**:
```python
{"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
```

**`CLIBridge._send(text, attachments=())`** — replaces `router.push()`:
```
POST {gateway_url}/v1/lane/message
{ "node_id": self._cursor, "text": "...", "stream": true }
```
Opens aiohttp SSE stream. Parses `data: {...}` lines. For each event calls
`handle_event(fake_event)`. On `done`, updates `self._cursor` to `node_id`
from the done payload, persists to `~/.tinyctx/cursors/cli`.

Fake event objects need only the fields `handle_event` reads:
`.text`, `.tool_name`, `.args`, `.call_id`, `.output`, `.is_error`, `.message`.
The routing fields (`tail_node_id`, `trace_id`, `lane_node_id`) are internal
and never read by the renderer.

**`/reset`**:
1. `POST /v1/lane/branch` with `{"parent_node_id": null}` → get new node_id
2. Update `self._cursor`, persist to cursor file
3. `POST /v1/lane/open` with new node_id to warm lane

**Module slash commands** — not available (no in-process router). `/help`
lists built-ins only.

**New entry points:**

```python
MANUAL_LAUNCH = True

async def run(gateway) -> None:
    """Called by main.py bridge loader — skipped before reaching here."""
    pass

async def run_detached(gateway_url: str, api_key: str,
                       options: dict | None = None) -> None:
    """Called by commands/launch.py."""
    bridge = CLIBridge(None, options=options or {})
    bridge._gateway_url = gateway_url
    bridge._api_key     = api_key

    async with aiohttp.ClientSession() as session:
        r = await session.post(
            f"{gateway_url}/v1/lane/open",
            json={"node_id": _load_cli_cursor_path()},
            headers=bridge._http_headers(),
        )
        data = await r.json()
        bridge._cursor = data["node_id"]
        _save_cli_cursor_path(bridge._cursor)

    await bridge.run()
```

`_load_cli_cursor_path()` / `_save_cli_cursor_path()` read/write
`~/.tinyctx/cursors/cli` — unchanged from current implementation.

---

## New files in detail

### `pid.py`

Manages `~/.tinyctx/daemon.pid` (JSON).

Fields: `pid`, `gateway_url`, `api_key`, `config_path`, `started_at`.

Functions: `write(...)`, `read() -> dict | None`, `is_alive(pid) -> bool`,
`clean()`.

### `commands/start.py`

1. Load config → extract gateway host/port/api_key.
2. Check existing pid: if alive → print URL and exit.
3. Clean stale pid if dead.
4. Spawn `main.py` detached (platform-specific). Log to `~/.tinyctx/daemon.log`.
5. Write pid file.
6. Poll `GET /v1/health` up to 8s. On success:
   ```
   ✓ TinyCTX running — http://127.0.0.1:8080
     API key: your-secret-token
     logs:    ~/.tinyctx/daemon.log
   ```
7. `--foreground` flag: skip detach.

### `commands/stop.py`

SIGTERM → poll 5s → SIGKILL → clean pid file.

### `commands/status.py`

Read pid → check alive → `GET /v1/health` → print.

### `commands/onboard.py`

```python
from onboard.__main__ import main as _onboard_main
def run(args):
    sys.argv = ["onboard"] + (["--reset"] if "--reset" in args else [])
    _onboard_main()
```

### `commands/launch.py`

`tinyctx launch cli`:
1. Read pid file → get gateway_url, api_key.
2. Load config for `bridges.cli.options`.
3. Call `bridges.cli.__main__.run_detached(gateway_url, api_key, options)`.

### `__main__.py`

Argparse dispatcher for: `onboard`, `start`, `stop`, `status`, `launch`.

---

## `pyproject.toml`

```toml
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "tinyctx"
version = "0.1.0"
requires-python = ">=3.11"

[project.scripts]
tinyctx = "cmd.__main__:main"
```

---

## What is NOT changing

| File / directory          | Status               |
|---------------------------|----------------------|
| `agent.py`                | untouched            |
| `ai.py`                   | untouched            |
| `context.py`              | untouched            |
| `router.py`               | untouched            |
| `db.py`                   | untouched            |
| `config/`                 | untouched            |
| `onboard/`                | untouched            |
| `modules/`                | untouched            |
| `bridges/discord/`        | untouched            |
| `bridges/matrix/`         | untouched            |
| `bridges/sillytavern/`    | untouched            |
| `config.yaml`             | untouched            |
| `contracts.py`            | +1 constant          |
| `main.py`                 | +3 lines             |
| `bridges/cli/__main__.py` | rewritten in-place   |
| `gateway/__main__.py`     | rewritten in-place   |

---

## Implementation order

1. `contracts.py` — add `MANUAL_LAUNCH_ATTR` constant
2. `main.py` — add `getattr(mod, "MANUAL_LAUNCH", False)` skip check
3. `gateway/__main__.py` — rewrite with 4-endpoint API + fanout table
4. `bridges/cli/__main__.py` — add `MANUAL_LAUNCH`, rewrite send/receive,
   add `run_detached()`
5. `pid.py`
6. `commands/start.py`
7. `commands/stop.py`
8. `commands/status.py`
9. `commands/onboard.py`
10. `commands/launch.py`
11. `__main__.py`
12. `pyproject.toml`

Steps 1–4 are the core rewrite. Steps 5–12 are all new files.

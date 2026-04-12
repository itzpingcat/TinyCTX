"""
gateway/__main__.py — HTTP/SSE API gateway (lane-based, node_id-keyed).

All endpoints except /v1/health require:
    Authorization: Bearer <api_key>

Endpoints
---------
POST   /v1/lane/open       Open (or no-op) a lane; bootstrap cursor if needed.
POST   /v1/lane/message    Push a user message; returns SSE event stream.
POST   /v1/lane/branch     Create a new branch node; return its node_id.
                           Non-destructive — no existing lane is modified.
DELETE /v1/lane/abort      Abort in-flight generation for a node_id.
POST   /v1/lane/command    Dispatch a slash command against the shared registry.
                           Body: { "node_id": "...", "text": "/memory consolidate" }
                           Returns: { "handled": true, "output": "..." }
                           The command handler captures its console output via a
                           lightweight StringConsole shim and returns it as JSON.
GET    /v1/commands        List all registered slash commands and their help text.
                           Returns: { "commands": [{ "command": "/memory consolidate",
                                                      "help": "..." }, ...] }
POST   /v1/shutdown        Gracefully shut down the daemon (auth required).
GET    /v1/health          Always public.

Kept from old gateway
---------------------
GET    /v1/workspace/files/{path}
PUT    /v1/workspace/files/{path}

SSE event types
---------------
  {"type": "thinking_chunk", "text": "..."}
  {"type": "text_chunk",     "text": "..."}
  {"type": "text_final",     "text": "..."}
  {"type": "tool_call",      "tool_name": "...", "call_id": "...", "args": {...}}
  {"type": "tool_result",    "tool_name": "...", "call_id": "...", "output": "...", "is_error": false}
  {"type": "outbound_files", "paths": [...]}
  {"type": "error",          "message": "..."}
  {"type": "done",           "node_id": "<new tail uuid>"}

Fanout table
------------
The gateway maintains a per-node_id fanout table so that multiple concurrent
SSE clients on the same node_id all receive every event. One persistent cursor
handler is registered with the router per active node_id; it fans events out
into per-request asyncio.Queue instances. When the last queue for a node_id
is removed the cursor handler is unregistered.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

from aiohttp import web

from TinyCTX.config import GatewayConfig
from TinyCTX.contracts import (
    Platform, ContentType, content_type_for,
    UserIdentity, InboundMessage, Attachment,
    AgentThinkingChunk, AgentTextChunk, AgentTextFinal,
    AgentToolCall, AgentToolResult, AgentError, AgentOutboundFiles,
)

logger = logging.getLogger(__name__)

_API_AUTHOR = UserIdentity(platform=Platform.API, user_id="api-client", username="api")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_workspace_path(workspace_root: Path, rel: str) -> Path | None:
    try:
        target = (workspace_root / rel).resolve()
        target.relative_to(workspace_root.resolve())
        return target
    except ValueError:
        return None


def _auth_middleware(api_key: str):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.path == "/v1/health":
            return await handler(request)
        if not api_key:
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != api_key:
            raise web.HTTPUnauthorized(
                content_type="application/json",
                body=json.dumps({"error": "invalid or missing api key"}),
            )
        return await handler(request)
    return middleware


def _event_to_dict(event) -> dict:
    """Convert an AgentEvent to a JSON-serialisable dict for SSE."""
    if isinstance(event, AgentThinkingChunk):
        return {"type": "thinking_chunk", "text": event.text}
    if isinstance(event, AgentTextChunk):
        return {"type": "text_chunk", "text": event.text}
    if isinstance(event, AgentTextFinal):
        return {"type": "text_final", "text": event.text, "node_id": event.tail_node_id}
    if isinstance(event, AgentToolCall):
        return {"type": "tool_call", "tool_name": event.tool_name,
                "call_id": event.call_id, "args": event.args}
    if isinstance(event, AgentToolResult):
        return {"type": "tool_result", "tool_name": event.tool_name,
                "call_id": event.call_id, "output": event.output,
                "is_error": event.is_error}
    if isinstance(event, AgentOutboundFiles):
        return {"type": "outbound_files", "paths": list(event.paths)}
    if isinstance(event, AgentError):
        return {"type": "error", "message": event.message, "node_id": event.tail_node_id}
    return {}


# ---------------------------------------------------------------------------
# Fanout table management
# ---------------------------------------------------------------------------

def _ensure_fanout(node_id: str, app: web.Application) -> None:
    """Register a persistent cursor handler for node_id if not already present."""
    fanout: dict[str, set[asyncio.Queue]] = app["fanout"]
    router = app["router"]

    if node_id in fanout:
        return  # handler already registered

    fanout[node_id] = set()

    async def _handler(event) -> None:
        payload = _event_to_dict(event)
        if not payload:
            return
        is_terminal = isinstance(event, (AgentTextFinal, AgentError))
        for q in list(fanout.get(node_id, [])):
            await q.put(("event", payload))
            if is_terminal:
                await q.put(("done", event.tail_node_id))

    router.register_cursor_handler(node_id, _handler)
    logger.debug("gateway: registered fanout handler for node_id=%s", node_id)


def _add_subscriber(node_id: str, app: web.Application) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    app["fanout"][node_id].add(q)
    return q


def _remove_subscriber(node_id: str, q: asyncio.Queue, app: web.Application) -> None:
    fanout: dict[str, set[asyncio.Queue]] = app["fanout"]
    router = app["router"]
    fanout.get(node_id, set()).discard(q)
    if not fanout.get(node_id):
        router.unregister_cursor_handler(node_id)
        fanout.pop(node_id, None)
        logger.debug("gateway: unregistered fanout handler for node_id=%s", node_id)


# ---------------------------------------------------------------------------
# POST /v1/lane/open
# ---------------------------------------------------------------------------

async def handle_lane_open(request: web.Request) -> web.Response:
    """
    Open (or no-op) a lane for node_id. If node_id is null/absent or
    unknown, create a fresh branch off DB root and return its id.
    """
    router    = request.app["router"]
    workspace = request.app["workspace"]

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    node_id = (body.get("node_id") or "").strip() or None

    from TinyCTX.db import ConversationDB
    db = ConversationDB(workspace / "agent.db")

    if node_id and db.get_node(node_id) is not None:
        router.open_lane(node_id, Platform.API.value)
        logger.debug("gateway: opened existing lane node_id=%s", node_id)
    else:
        root = db.get_root()
        node = db.add_node(parent_id=root.id, role="system", content="session:api")
        node_id = node.id
        router.open_lane(node_id, Platform.API.value)
        logger.info("gateway: created new lane node_id=%s", node_id)

    return web.Response(
        content_type="application/json",
        body=json.dumps({"node_id": node_id}),
    )


# ---------------------------------------------------------------------------
# POST /v1/lane/message
# ---------------------------------------------------------------------------

async def handle_lane_message(request: web.Request) -> web.StreamResponse:
    """
    Push a user message for node_id and stream the reply via SSE.
    The final SSE event is {"type": "done", "node_id": "<new tail>"}.
    """
    router    = request.app["router"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "invalid JSON"}))

    node_id = (body.get("node_id") or "").strip()
    if not node_id:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "node_id required"}))

    text = body.get("text", "").strip()
    if not text and not body.get("attachments"):
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "text or attachments required"}))

    raw_atts = body.get("attachments") or []
    attachments: tuple[Attachment, ...] = ()
    if raw_atts:
        parsed = []
        for item in raw_atts:
            try:
                data = base64.b64decode(item["data_b64"])
            except Exception:
                raise web.HTTPBadRequest(
                    content_type="application/json",
                    body=json.dumps({"error": f"invalid base64 in '{item.get('name', '?')}'"}),
                )
            parsed.append(Attachment(
                filename=item.get("name", "file"),
                data=data,
                mime_type=item.get("mime_type", "application/octet-stream"),
            ))
        attachments = tuple(parsed)

    msg = InboundMessage(
        tail_node_id=node_id,
        author=_API_AUTHOR,
        content_type=content_type_for(text, bool(attachments)),
        text=text,
        message_id=str(time.time_ns()),
        timestamp=time.time(),
        attachments=attachments,
    )

    # Set up fanout before pushing so no events are missed.
    _ensure_fanout(node_id, request.app)
    q = _add_subscriber(node_id, request.app)

    accepted = await router.push(msg)
    if not accepted:
        _remove_subscriber(node_id, q, request.app)
        raise web.HTTPTooManyRequests(content_type="application/json",
                                      body=json.dumps({"error": "lane queue full"}))

    # Stream SSE response.
    response = web.StreamResponse(headers={
        "Content-Type":      "text/event-stream",
        "Cache-Control":     "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    try:
        while True:
            kind, payload = await q.get()
            if kind == "event":
                try:
                    await response.write(f"data: {json.dumps(payload)}\n\n".encode())
                except (ConnectionResetError, Exception) as exc:
                    logger.debug("gateway: client disconnected mid-stream for node_id=%s (%s)", node_id, exc)
                    break
            elif kind == "done":
                new_tail = payload  # tail node_id from the terminal event
                try:
                    await response.write(
                        f'data: {json.dumps({"type": "done", "node_id": new_tail})}\n\n'.encode()
                    )
                except Exception:
                    pass
                break
    except asyncio.CancelledError:
        pass
    finally:
        _remove_subscriber(node_id, q, request.app)

    await response.write_eof()
    return response


# ---------------------------------------------------------------------------
# POST /v1/lane/branch
# ---------------------------------------------------------------------------

async def handle_lane_branch(request: web.Request) -> web.Response:
    """
    Create a new child node in agent.db and return its node_id.
    Non-destructive — no existing lane is reset or modified.

    Body: { "parent_node_id": "<uuid or null>" }

    parent_node_id: node to branch from. If null/absent, branches off DB root.
    """
    workspace = request.app["workspace"]

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    parent_node_id = (body.get("parent_node_id") or "").strip() or None

    from TinyCTX.db import ConversationDB
    db   = ConversationDB(workspace / "agent.db")
    root = db.get_root()

    if parent_node_id and db.get_node(parent_node_id) is not None:
        parent_id = parent_node_id
    else:
        parent_id = root.id

    node = db.add_node(parent_id=parent_id, role="system", content="session:branch")
    logger.info("gateway: branched node_id=%s from parent=%s", node.id, parent_id)

    return web.Response(
        content_type="application/json",
        body=json.dumps({"node_id": node.id}),
    )


# ---------------------------------------------------------------------------
# DELETE /v1/lane/abort
# ---------------------------------------------------------------------------

async def handle_lane_abort(request: web.Request) -> web.Response:
    """Abort in-flight generation for node_id. No-op if nothing is running."""
    router = request.app["router"]

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    node_id = (body.get("node_id") or "").strip()
    if not node_id:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "node_id required"}))

    router.abort_generation(node_id)
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# Slash-command helpers
# ---------------------------------------------------------------------------

class _StringConsole:
    """
    Minimal Rich Console shim that captures print() calls as plain text.

    CommandRegistry handlers receive a ``context`` dict with a "console" key
    that is normally a rich.console.Console.  This shim accepts the same
    ``console.print(markup_string, ...)`` signature that all built-in handlers
    use and strips Rich markup tags so the returned output is clean text.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def print(self, *args, **kwargs) -> None:  # noqa: A003
        # Concatenate positional args (same as rich.Console.print behaviour).
        raw = " ".join(str(a) for a in args)
        # Strip Rich markup tags  [color]...[/color]  →  ...
        import re
        clean = re.sub(r"\[/?[^\[\]]*\]", "", raw).strip()
        if clean:
            self._lines.append(clean)

    def get_output(self) -> str:
        return "\n".join(self._lines)


# ---------------------------------------------------------------------------
# POST /v1/lane/command
# ---------------------------------------------------------------------------

async def handle_lane_command(request: web.Request) -> web.Response:
    """
    Dispatch a slash command against the router's shared CommandRegistry.

    Body
    ----
    {
        "node_id": "<cursor uuid>",          // required
        "text":    "/memory consolidate"     // required — must start with /
    }

    Response (200)
    --------------
    { "handled": true,  "output": "✓  memory consolidation started (branch off …)" }
    { "handled": false, "output": "" }   // unknown command

    Response (400)
    --------------
    { "error": "..." }   // missing field or text doesn't start with /
    """
    router = request.app["router"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "invalid JSON"}))

    node_id = (body.get("node_id") or "").strip()
    if not node_id:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "node_id required"}))

    text = (body.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "text required"}))
    if not text.startswith("/"):
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "text must start with /"}))

    # Build the context dict that command handlers expect.
    console = _StringConsole()

    # Resolve the agent attached to this lane (if open) so handlers that
    # need agent._db / agent.context work correctly.
    lane = router._lane_router._lanes.get(node_id)
    agent = lane.loop if lane is not None else None

    context: dict = {
        "node_id":   node_id,
        "console":   console,
        "gateway":   router,
        "agent":     agent,
        # Neutral theme_c — returns empty string for all keys so handlers
        # that call c("tool_ok") etc. get plain text from _StringConsole.
        "theme_c":   lambda _k: "",
    }

    handled = await router.commands.dispatch(text, context)
    output  = console.get_output()

    logger.info(
        "gateway: /v1/lane/command node_id=%s text=%r handled=%s",
        node_id, text, handled,
    )

    return web.Response(
        content_type="application/json",
        body=json.dumps({"handled": handled, "output": output}),
    )


# ---------------------------------------------------------------------------
# GET /v1/commands
# ---------------------------------------------------------------------------

async def handle_commands_list(request: web.Request) -> web.Response:
    """
    Return all registered slash commands and their one-line help strings.

    Response (200)
    --------------
    {
        "commands": [
            { "command": "/memory consolidate", "help": "Spawn a memory consolidation branch immediately" },
            ...
        ]
    }
    """
    router = request.app["router"]
    rows = router.commands.list_commands()
    return web.Response(
        content_type="application/json",
        body=json.dumps({"commands": [{"command": cmd, "help": hlp} for cmd, hlp in rows]}),
    )


# ---------------------------------------------------------------------------
# POST /v1/shutdown
# ---------------------------------------------------------------------------

async def handle_shutdown(request: web.Request) -> web.Response:
    """
    Gracefully shut down the daemon by setting the app-level shutdown event.
    Returns 204 immediately; the daemon exits after the response is sent.
    """
    logger.info("gateway: shutdown requested via /v1/shutdown")
    # Schedule the event to fire after the response is flushed.
    loop = asyncio.get_event_loop()
    shutdown_event: asyncio.Event = request.app["shutdown_event"]
    loop.call_soon(shutdown_event.set)
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

async def handle_workspace_get(request: web.Request) -> web.Response:
    workspace = request.app["workspace"]
    rel       = request.match_info["path"]
    target    = _resolve_workspace_path(workspace, rel)
    if target is None:
        raise web.HTTPForbidden(content_type="application/json",
                                body=json.dumps({"error": "path escapes workspace root"}))
    if not target.exists() or not target.is_file():
        raise web.HTTPNotFound(content_type="application/json",
                               body=json.dumps({"error": "file not found"}))
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:
        raise web.HTTPInternalServerError(content_type="application/json",
                                          body=json.dumps({"error": str(exc)}))
    return web.Response(content_type="application/json",
                        body=json.dumps({"path": rel, "content": content}))


async def handle_workspace_put(request: web.Request) -> web.Response:
    workspace = request.app["workspace"]
    rel       = request.match_info["path"]
    target    = _resolve_workspace_path(workspace, rel)
    if target is None:
        raise web.HTTPForbidden(content_type="application/json",
                                body=json.dumps({"error": "path escapes workspace root"}))
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "invalid JSON"}))
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "content required"}))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except Exception as exc:
        raise web.HTTPInternalServerError(content_type="application/json",
                                          body=json.dumps({"error": str(exc)}))
    return web.Response(content_type="application/json",
                        body=json.dumps({"path": rel, "written": True}))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    router = request.app["router"]
    uptime = time.time() - request.app["start_time"]
    fanout: dict = request.app["fanout"]

    lanes_summary = {}
    for node_id, lane in router._lane_router._lanes.items():
        lanes_summary[node_id] = {
            "turns":       lane.loop._turn_count,
            "queue_depth": lane.queue.qsize(),
            "queue_max":   lane.queue.maxsize,
            "subscribers": len(fanout.get(node_id, [])),
        }

    return web.Response(
        content_type="application/json",
        body=json.dumps({
            "status":   "ok",
            "uptime_s": round(uptime, 1),
            "lanes":    lanes_summary,
        }),
    )


# ---------------------------------------------------------------------------
# App factory + entrypoint
# ---------------------------------------------------------------------------

def _make_app(router, cfg: GatewayConfig, shutdown_event: asyncio.Event) -> web.Application:
    workspace = Path(router._config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    app = web.Application(middlewares=[_auth_middleware(cfg.api_key)])
    app["router"]         = router
    app["workspace"]      = workspace
    app["start_time"]     = time.time()
    app["fanout"]         = {}   # node_id -> set[asyncio.Queue]
    app["shutdown_event"] = shutdown_event

    # Lane API
    app.router.add_post(  "/v1/lane/open",                handle_lane_open)
    app.router.add_post(  "/v1/lane/message",             handle_lane_message)
    app.router.add_post(  "/v1/lane/branch",              handle_lane_branch)
    app.router.add_delete("/v1/lane/abort",               handle_lane_abort)
    app.router.add_post(  "/v1/lane/command",             handle_lane_command)

    # Command discovery
    app.router.add_get(   "/v1/commands",                 handle_commands_list)

    # Shutdown
    app.router.add_post(  "/v1/shutdown",                 handle_shutdown)

    # Workspace (kept)
    app.router.add_get(   "/v1/workspace/files/{path:.+}", handle_workspace_get)
    app.router.add_put(   "/v1/workspace/files/{path:.+}", handle_workspace_put)

    # Health (public)
    app.router.add_get(   "/v1/health",                   handle_health)

    return app


async def run(router, cfg: GatewayConfig) -> None:
    shutdown_event = asyncio.Event()
    app    = _make_app(router, cfg, shutdown_event)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    logger.info("Gateway listening on http://%s:%d", cfg.host, cfg.port)
    try:
        await shutdown_event.wait()
        logger.info("Gateway shutdown event received — stopping.")
    finally:
        await runner.cleanup()

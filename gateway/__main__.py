"""
gateway/__main__.py — HTTP/SSE API gateway.

All endpoints require:  Authorization: Bearer <api_key>
/v1/health is always public.

Session model (Phase 2 tree refactor)
--------------------------------------
session_id is a human-readable string supplied by the API caller (e.g. "main",
"user-123"). It maps to a node_id (UUID) in agent.db via
workspace/cursors/gateway.json. On first use the gateway creates a child node
off the global DB root and persists the mapping. Subsequent calls reuse the
same node_id, which is passed as tail_node_id into InboundMessage.

Endpoints:

  POST   /v1/sessions/{session_id}/message
    body: { "text": "...", "stream": true,
            "attachments": [{"name", "data_b64", "mime_type"}] }
    SSE stream or JSON response.

  PUT    /v1/sessions/{session_id}/generation
    Queue a generation cycle with no new user message.
    body: { "stream": true }  (optional, default true)
    SSE stream identical to /message, or JSON { "text": "..." }.
    Returns 429 if lane queue is full.

  DELETE /v1/sessions/{session_id}/generation
    Abort the current in-flight generation. No-op (204) if nothing is running.
    -> 204

  GET    /v1/sessions
    List all sessions (active in-memory lanes + gateway cursor map).
    -> [{ "id", "node_id", "turns", "queue_depth", "queue_max", "is_active" }, ...]

  DELETE /v1/sessions/{session_id}
    Reset the lane in-memory (clears context). Tree in agent.db is preserved.
    -> 204

  POST   /v1/sessions/{session_id}/reset
    Alias for DELETE — backwards compat.
    -> 204

  GET    /v1/sessions/{session_id}/history
    Return the ancestor chain for this session's node_id as history entries.
    -> [{ "id", "role", "content", "tool_calls", "tool_call_id", "author_id", "created_at" }, ...]

  GET    /v1/workspace/files/{path}
    -> { "path": "...", "content": "..." }

  PUT    /v1/workspace/files/{path}
    body: { "content": "..." }
    -> { "path": "...", "written": true }

  GET    /v1/health
    -> { "status": "ok", "uptime_s": N, "lanes": { "<node_id>": {...} } }
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

from aiohttp import web

from config import GatewayConfig
from contracts import (
    Platform, ContentType, content_type_for,
    UserIdentity, InboundMessage, Attachment,
    AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError,
)

logger = logging.getLogger(__name__)

_API_AUTHOR = UserIdentity(platform=Platform.API, user_id="api-client", username="api")


# ---------------------------------------------------------------------------
# Cursor map — session_id (str) <-> node_id (UUID str)
# Persisted at workspace/cursors/gateway.json
# ---------------------------------------------------------------------------

def _load_cursor_map(cursors_dir: Path) -> dict[str, str]:
    path = cursors_dir / "gateway.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("gateway: corrupt cursor map — starting fresh")
    return {}


def _save_cursor_map(cursors_dir: Path, mapping: dict[str, str]) -> None:
    path = cursors_dir / "gateway.json"
    path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")


def _resolve_node_id(session_id: str, app: web.Application) -> str:
    """
    Return the node_id for session_id, creating a new child node off the
    global DB root on first use and persisting the mapping.
    """
    mapping     = app["cursor_map"]
    cursors_dir = app["cursors_dir"]

    if session_id in mapping:
        return mapping[session_id]

    # First use — create a child node off root and persist.
    from db import ConversationDB
    db   = ConversationDB(app["workspace"] / "agent.db")
    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content=f"session:gateway:{session_id}")
    mapping[session_id] = node.id
    _save_cursor_map(cursors_dir, mapping)
    logger.info("gateway: created cursor for session '%s' -> %s", session_id, node.id)
    return node.id


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


def _lane_for(router, node_id: str):
    """Return the Lane for node_id, or None."""
    return router._lane_router._lanes.get(node_id)


def _lane_summary(session_id: str, node_id: str, lane) -> dict:
    return {
        "id":          session_id,
        "node_id":     node_id,
        "turns":       lane.loop._turn_count,
        "queue_depth": lane.queue.qsize(),
        "queue_max":   lane.queue.maxsize,
        "is_active":   True,
    }


# ---------------------------------------------------------------------------
# SSE streaming shared helper
# ---------------------------------------------------------------------------

async def _stream_generation(request: web.Request, router, node_id: str) -> web.StreamResponse:
    """
    Register a per-cursor SSE handler, wait for generation to complete,
    and stream events back to the HTTP client.
    """
    response = web.StreamResponse(headers={
        "Content-Type":      "text/event-stream",
        "Cache-Control":     "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    done_event       = asyncio.Event()
    streamed_chunks: list[str] = []

    async def _sse(event) -> None:
        if isinstance(event, AgentTextChunk):
            data = {"type": "text_chunk", "text": event.text}
            streamed_chunks.append(event.text)
        elif isinstance(event, AgentTextFinal):
            data = {"type": "text_final", "text": event.text or "".join(streamed_chunks)}
            done_event.set()
        elif isinstance(event, AgentToolCall):
            data = {"type": "tool_call", "call_id": event.call_id,
                    "name": event.tool_name, "args": event.args}
        elif isinstance(event, AgentToolResult):
            data = {"type": "tool_result", "call_id": event.call_id,
                    "name": event.tool_name, "output": event.output,
                    "is_error": event.is_error}
        elif isinstance(event, AgentError):
            data = {"type": "error", "message": event.message}
            done_event.set()
        else:
            return
        try:
            await response.write(f"data: {json.dumps(data)}\n\n".encode())
        except Exception:
            done_event.set()

    router.register_cursor_handler(node_id, _sse)
    try:
        await done_event.wait()
        await response.write(b'data: {"type": "done"}\n\n')
    finally:
        router.unregister_cursor_handler(node_id)

    await response.write_eof()
    return response


async def _collect_generation(router, node_id: str) -> str:
    """Non-streaming: collect full text from a generation."""
    parts:      list[str] = []
    done_event            = asyncio.Event()

    async def _collect(event) -> None:
        if isinstance(event, AgentTextChunk):
            parts.append(event.text)
        elif isinstance(event, AgentTextFinal):
            if event.text:
                parts.append(event.text)
            done_event.set()
        elif isinstance(event, AgentError):
            parts.append(event.message)
            done_event.set()

    router.register_cursor_handler(node_id, _collect)
    try:
        await asyncio.wait_for(done_event.wait(), timeout=120)
    except asyncio.TimeoutError:
        pass
    finally:
        router.unregister_cursor_handler(node_id)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Message handler (POST /v1/sessions/{id}/message)
# ---------------------------------------------------------------------------

async def handle_message(request: web.Request) -> web.StreamResponse:
    router     = request.app["router"]
    session_id = request.match_info["session_id"]
    node_id    = _resolve_node_id(session_id, request.app)

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "invalid JSON"}))

    text      = body.get("text", "").strip()
    do_stream = bool(body.get("stream", True))

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

    accepted = await router.push(msg)
    if not accepted:
        raise web.HTTPTooManyRequests(content_type="application/json",
                                      body=json.dumps({"error": "session queue full"}))

    if do_stream:
        return await _stream_generation(request, router, node_id)
    else:
        text_out = await _collect_generation(router, node_id)
        return web.Response(content_type="application/json", body=json.dumps({"text": text_out}))


# ---------------------------------------------------------------------------
# Synthetic generation (PUT /v1/sessions/{id}/generation)
# ---------------------------------------------------------------------------

async def handle_generation_put(request: web.Request) -> web.Response:
    """
    Queue a generation cycle against the current context, with no new user
    message. Use to trigger a fresh response without adding a user turn.
    """
    router     = request.app["router"]
    session_id = request.match_info["session_id"]
    node_id    = _resolve_node_id(session_id, request.app)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass  # body is optional

    do_stream = bool(body.get("stream", True))

    accepted = await router.push_synthetic(node_id)
    if not accepted:
        raise web.HTTPTooManyRequests(content_type="application/json",
                                      body=json.dumps({"error": "session queue full"}))

    if do_stream:
        return await _stream_generation(request, router, node_id)
    else:
        text_out = await _collect_generation(router, node_id)
        return web.Response(content_type="application/json", body=json.dumps({"text": text_out}))


# ---------------------------------------------------------------------------
# Abort (DELETE /v1/sessions/{id}/generation)
# ---------------------------------------------------------------------------

async def handle_generation_delete(request: web.Request) -> web.Response:
    """Abort the current in-flight generation. No-op if nothing is running."""
    router     = request.app["router"]
    session_id = request.match_info["session_id"]
    node_id    = _resolve_node_id(session_id, request.app)
    router.abort_generation(node_id)
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# Session list (GET /v1/sessions)
# ---------------------------------------------------------------------------

async def handle_sessions_list(request: web.Request) -> web.Response:
    router  = request.app["router"]
    mapping = request.app["cursor_map"]  # session_id -> node_id

    results: list[dict] = []

    # Start from the known gateway cursor map — every named session appears here.
    for session_id, node_id in sorted(mapping.items()):
        lane = _lane_for(router, node_id)
        if lane is not None:
            results.append(_lane_summary(session_id, node_id, lane))
        else:
            results.append({
                "id":          session_id,
                "node_id":     node_id,
                "turns":       None,
                "queue_depth": 0,
                "queue_max":   0,
                "is_active":   False,
            })

    # Also surface any active lanes not in the cursor map (e.g. other bridges).
    reverse = {v: k for k, v in mapping.items()}
    for node_id in router.active_lanes:
        if node_id in reverse:
            continue  # already listed above
        lane = _lane_for(router, node_id)
        if lane:
            results.append({
                "id":          node_id,   # no human name — use node_id as id
                "node_id":     node_id,
                "turns":       lane.loop._turn_count,
                "queue_depth": lane.queue.qsize(),
                "queue_max":   lane.queue.maxsize,
                "is_active":   True,
            })

    return web.Response(content_type="application/json", body=json.dumps(results))


# ---------------------------------------------------------------------------
# Session reset / delete
# ---------------------------------------------------------------------------

async def handle_session_delete(request: web.Request) -> web.Response:
    """Reset the lane's in-memory context. Tree in agent.db is preserved."""
    router     = request.app["router"]
    session_id = request.match_info["session_id"]
    node_id    = _resolve_node_id(session_id, request.app)
    router.reset_lane(node_id)
    return web.Response(status=204)


async def handle_session_reset(request: web.Request) -> web.Response:
    """Alias for DELETE — backwards compat."""
    return await handle_session_delete(request)


# ---------------------------------------------------------------------------
# History (GET /v1/sessions/{id}/history)
# ---------------------------------------------------------------------------

async def handle_history_get(request: web.Request) -> web.Response:
    """
    Return the ancestor chain for this session's node_id.
    Reads directly from agent.db — no lane needs to be active.
    """
    session_id = request.match_info["session_id"]
    node_id    = _resolve_node_id(session_id, request.app)

    from db import ConversationDB
    db      = ConversationDB(request.app["workspace"] / "agent.db")
    nodes   = db.get_ancestors(node_id)  # root -> current tail order
    entries = [
        {
            "id":           n.id,
            "role":         n.role,
            "content":      n.content,
            "tool_calls":   n.tool_calls,
            "tool_call_id": n.tool_call_id,
            "author_id":    n.author_id,
            "created_at":   n.created_at,
        }
        for n in nodes
        if n.role != "system"  # skip session-marker nodes
    ]
    return web.Response(content_type="application/json", body=json.dumps(entries))


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
    return web.Response(content_type="application/json", body=json.dumps({"path": rel, "content": content}))


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
    return web.Response(content_type="application/json", body=json.dumps({"path": rel, "written": True}))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    router  = request.app["router"]
    uptime  = time.time() - request.app["start_time"]
    mapping = request.app["cursor_map"]
    reverse = {v: k for k, v in mapping.items()}  # node_id -> session_id

    payload = {
        "status":   "ok",
        "uptime_s": round(uptime, 1),
        "lanes": {
            node_id: {
                "session_id":  reverse.get(node_id, node_id),
                "turns":       lane.loop._turn_count,
                "queue_depth": lane.queue.qsize(),
                "queue_max":   lane.queue.maxsize,
            }
            for node_id, lane in router._lane_router._lanes.items()
        },
    }
    return web.Response(content_type="application/json", body=json.dumps(payload))


# ---------------------------------------------------------------------------
# App factory + entrypoint
# ---------------------------------------------------------------------------

def _make_app(router, cfg: GatewayConfig) -> web.Application:
    workspace   = Path(router._config.workspace.path).expanduser().resolve()
    cursors_dir = workspace / "cursors"
    cursors_dir.mkdir(parents=True, exist_ok=True)

    app = web.Application(middlewares=[_auth_middleware(cfg.api_key)])
    app["router"]      = router
    app["workspace"]   = workspace
    app["cursors_dir"] = cursors_dir
    app["cursor_map"]  = _load_cursor_map(cursors_dir)  # mutable dict, saved on write
    app["start_time"]  = time.time()

    # Session management
    app.router.add_get(   "/v1/sessions",                                  handle_sessions_list)
    app.router.add_delete("/v1/sessions/{session_id}",                     handle_session_delete)

    # Generation
    app.router.add_post(  "/v1/sessions/{session_id}/message",             handle_message)
    app.router.add_put(   "/v1/sessions/{session_id}/generation",          handle_generation_put)
    app.router.add_delete("/v1/sessions/{session_id}/generation",          handle_generation_delete)

    # Session lifecycle
    app.router.add_post(  "/v1/sessions/{session_id}/reset",               handle_session_reset)

    # History
    app.router.add_get(   "/v1/sessions/{session_id}/history",             handle_history_get)

    # Workspace
    app.router.add_get(   "/v1/workspace/files/{path:.+}",                 handle_workspace_get)
    app.router.add_put(   "/v1/workspace/files/{path:.+}",                 handle_workspace_put)

    # Health (public)
    app.router.add_get(   "/v1/health",                                    handle_health)

    return app


async def run(router, cfg: GatewayConfig) -> None:
    app    = _make_app(router, cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    logger.info("Gateway listening on http://%s:%d", cfg.host, cfg.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()

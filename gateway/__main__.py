"""
gateway/__main__.py — HTTP/SSE API gateway.

Exposes run(router, config) for main.py.

All endpoints require:  Authorization: Bearer <api_key>

Endpoints:

  POST   /v1/sessions/{session_id}/message
    body: { "text": "...", "stream": true, "session_type": "dm"|"group" }
    SSE stream:
      data: {"type": "text_chunk",   "text": "..."}
      data: {"type": "tool_call",    "call_id": "...", "name": "...", "args": {...}}
      data: {"type": "tool_result",  "call_id": "...", "name": "...", "output": "...", "is_error": false}
      data: {"type": "text_final",   "text": "..."}
      data: {"type": "error",        "message": "..."}
      data: {"type": "done"}
    non-stream: { "text": "..." }

  GET    /v1/sessions/{session_id}/history
    query: ?session_type=dm|group  (default: dm)
    -> [{ "id", "role", "content", "tool_calls", "tool_call_id", "index" }, ...]

  PATCH  /v1/sessions/{session_id}/history/{entry_id}
    body: { "content": "..." }
    -> { "updated": true }

  DELETE /v1/sessions/{session_id}/history/{entry_id}
    -> { "removed": ["id", ...] }

  POST   /v1/sessions/{session_id}/reset
    query: ?session_type=dm|group  (default: dm)
    -> 204

  GET    /v1/workspace/files/{path}
    -> { "path": "...", "content": "..." }

  PUT    /v1/workspace/files/{path}
    body: { "content": "..." }
    -> { "path": "...", "written": true }

  GET    /v1/health
    -> { "status": "ok", "sessions": [...] }

Session type / key resolution:
  dm    -> SessionKey.dm(session_id)               platform=API (via author)
  group -> SessionKey.group(Platform.API, session_id)

Path safety for workspace files:
  Resolved path must be under workspace root. Requests that escape via ".."
  are rejected with 403.
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
    SessionKey, UserIdentity, InboundMessage, Attachment,
    AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError,
)

logger = logging.getLogger(__name__)

_API_AUTHOR = UserIdentity(
    platform=Platform.API,
    user_id="api-client",
    username="api",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_key(session_id: str, session_type: str) -> SessionKey:
    if session_type == "group":
        return SessionKey.group(Platform.API, session_id)
    return SessionKey.dm(session_id)


def _resolve_workspace_path(workspace_root: Path, rel: str) -> Path | None:
    """
    Resolve a relative path under workspace_root.
    Returns None if the resolved path escapes the root (path traversal attempt).
    """
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


def _lane_for(router, key: SessionKey):
    return router._session_router._lanes.get(key)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def handle_message(request: web.Request) -> web.StreamResponse:
    router     = request.app["router"]
    session_id = request.match_info["session_id"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": "invalid JSON"}),
        )

    text         = body.get("text", "").strip()
    do_stream    = bool(body.get("stream", True))
    session_type = body.get("session_type", "dm")

    if not text and not body.get("attachments"):
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": "text or attachments is required"}),
        )

    # Parse attachments: [{"name": str, "data_b64": str, "mime_type": str}, ...]
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
                    body=json.dumps({"error": f"invalid base64 in attachment '{item.get('name', '?')}'"}),
                )
            parsed.append(Attachment(
                filename=item.get("name", "file"),
                data=data,
                mime_type=item.get("mime_type", "application/octet-stream"),
            ))
        attachments = tuple(parsed)

    sk = _session_key(session_id, session_type)
    msg = InboundMessage(
        session_key=sk,
        author=_API_AUTHOR,
        content_type=content_type_for(text, bool(attachments)),
        text=text,
        message_id=str(time.time_ns()),
        timestamp=time.time(),
        attachments=attachments,
    )

    if do_stream:
        response = web.StreamResponse(headers={
            "Content-Type":      "text/event-stream",
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)

        done_event      = asyncio.Event()
        streamed_chunks: list[str] = []

        async def _sse_handler(event) -> None:
            if isinstance(event, AgentTextChunk):
                data = {"type": "text_chunk", "text": event.text}
                streamed_chunks.append(event.text)
            elif isinstance(event, AgentTextFinal):
                text_val = event.text if event.text else "".join(streamed_chunks)
                data = {"type": "text_final", "text": text_val}
                done_event.set()
            elif isinstance(event, AgentToolCall):
                data = {
                    "type":    "tool_call",
                    "call_id": event.call_id,
                    "name":    event.tool_name,
                    "args":    event.args,
                }
            elif isinstance(event, AgentToolResult):
                data = {
                    "type":     "tool_result",
                    "call_id":  event.call_id,
                    "name":     event.tool_name,
                    "output":   event.output,
                    "is_error": event.is_error,
                }
            elif isinstance(event, AgentError):
                data = {"type": "error", "message": event.message}
                done_event.set()
            else:
                return

            try:
                await response.write(f"data: {json.dumps(data)}\n\n".encode())
            except Exception:
                done_event.set()

        router.register_session_handler(sk, _sse_handler)
        try:
            accepted = await router.push(msg)
            if not accepted:
                router.unregister_session_handler(sk)
                raise web.HTTPTooManyRequests(
                    content_type="application/json",
                    body=json.dumps({"error": "session queue full, try again later"}),
                )
            await done_event.wait()
            await response.write(b'data: {"type": "done"}\n\n')
        finally:
            router.unregister_session_handler(sk)

        await response.write_eof()
        return response

    else:
        parts:     list[str] = []
        done_event           = asyncio.Event()

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

        router.register_session_handler(sk, _collect)
        try:
            accepted = await router.push(msg)
            if not accepted:
                router.unregister_session_handler(sk)
                raise web.HTTPTooManyRequests(
                    content_type="application/json",
                    body=json.dumps({"error": "session queue full, try again later"}),
                )
            await asyncio.wait_for(done_event.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        finally:
            router.unregister_session_handler(sk)

        return web.Response(
            content_type="application/json",
            body=json.dumps({"text": "".join(parts)}),
        )


async def handle_history_get(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)

    lane = _lane_for(router, sk)
    if lane is None:
        return web.Response(content_type="application/json", body=json.dumps([]))

    entries = [
        {
            "id":           e.id,
            "role":         e.role,
            "content":      e.content,
            "tool_calls":   e.tool_calls,
            "tool_call_id": e.tool_call_id,
            "index":        e.index,
            "author_id":    e.author_id,
        }
        for e in list(lane.loop.context.dialogue)
    ]
    return web.Response(content_type="application/json", body=json.dumps(entries))


async def handle_history_patch(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    entry_id     = request.match_info["entry_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": "invalid JSON"}),
        )

    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": "content is required"}),
        )

    lane = _lane_for(router, sk)
    if lane is None:
        raise web.HTTPNotFound(
            content_type="application/json",
            body=json.dumps({"error": "session not found"}),
        )

    if not lane.loop.context.edit(entry_id, content):
        raise web.HTTPNotFound(
            content_type="application/json",
            body=json.dumps({"error": "entry not found"}),
        )

    return web.Response(content_type="application/json", body=json.dumps({"updated": True}))


async def handle_history_delete(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    entry_id     = request.match_info["entry_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)

    lane = _lane_for(router, sk)
    if lane is None:
        raise web.HTTPNotFound(
            content_type="application/json",
            body=json.dumps({"error": "session not found"}),
        )

    removed = lane.loop.context.delete(entry_id)
    if not removed:
        raise web.HTTPNotFound(
            content_type="application/json",
            body=json.dumps({"error": "entry not found"}),
        )

    return web.Response(content_type="application/json", body=json.dumps({"removed": removed}))


async def handle_session_reset(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)
    router.reset_session(sk)
    return web.Response(status=204)


async def handle_workspace_get(request: web.Request) -> web.Response:
    workspace = request.app["workspace"]
    rel       = request.match_info["path"]
    target    = _resolve_workspace_path(workspace, rel)

    if target is None:
        raise web.HTTPForbidden(
            content_type="application/json",
            body=json.dumps({"error": "path escapes workspace root"}),
        )
    if not target.exists() or not target.is_file():
        raise web.HTTPNotFound(
            content_type="application/json",
            body=json.dumps({"error": "file not found"}),
        )

    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:
        raise web.HTTPInternalServerError(
            content_type="application/json",
            body=json.dumps({"error": str(exc)}),
        )

    return web.Response(content_type="application/json", body=json.dumps({"path": rel, "content": content}))


async def handle_workspace_put(request: web.Request) -> web.Response:
    workspace = request.app["workspace"]
    rel       = request.match_info["path"]
    target    = _resolve_workspace_path(workspace, rel)

    if target is None:
        raise web.HTTPForbidden(
            content_type="application/json",
            body=json.dumps({"error": "path escapes workspace root"}),
        )

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": "invalid JSON"}),
        )

    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": "content is required"}),
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except Exception as exc:
        raise web.HTTPInternalServerError(
            content_type="application/json",
            body=json.dumps({"error": str(exc)}),
        )

    return web.Response(content_type="application/json", body=json.dumps({"path": rel, "written": True}))


async def handle_health(request: web.Request) -> web.Response:
    router  = request.app["router"]
    lanes   = router._session_router._lanes
    uptime  = time.time() - request.app["start_time"]
    payload = {
        "status":   "ok",
        "uptime_s": round(uptime, 1),
        "sessions": {
            str(sk): {
                "queue_depth": lane.queue.qsize(),
                "queue_max":   lane.queue.maxsize,
                "turns":       lane.loop._turn_count,
            }
            for sk, lane in lanes.items()
        },
    }
    return web.Response(content_type="application/json", body=json.dumps(payload))


# ---------------------------------------------------------------------------
# App factory + entrypoint
# ---------------------------------------------------------------------------

def _make_app(router, cfg: GatewayConfig) -> web.Application:
    workspace = Path(router._config.workspace.path).expanduser().resolve()

    app = web.Application(middlewares=[_auth_middleware(cfg.api_key)])
    app["router"]     = router
    app["workspace"]  = workspace
    app["start_time"] = time.time()

    app.router.add_post(  "/v1/sessions/{session_id}/message",            handle_message)
    app.router.add_get(   "/v1/sessions/{session_id}/history",            handle_history_get)
    app.router.add_patch( "/v1/sessions/{session_id}/history/{entry_id}", handle_history_patch)
    app.router.add_delete("/v1/sessions/{session_id}/history/{entry_id}", handle_history_delete)
    app.router.add_post(  "/v1/sessions/{session_id}/reset",              handle_session_reset)
    app.router.add_get(   "/v1/workspace/files/{path:.+}",                handle_workspace_get)
    app.router.add_put(   "/v1/workspace/files/{path:.+}",                handle_workspace_put)
    app.router.add_get(   "/v1/health",                                   handle_health)

    return app


async def run(router, cfg: GatewayConfig) -> None:
    """Entry point called by main.py."""
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

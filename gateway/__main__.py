"""
gateway/__main__.py — HTTP/SSE API gateway.

All endpoints require:  Authorization: Bearer <api_key>
/v1/health is always public.

Endpoints:

  POST   /v1/sessions/{session_id}/message
    body: { "text": "...", "stream": true, "session_type": "dm"|"group",
            "attachments": [{"name", "data_b64", "mime_type"}] }
    SSE or JSON response.

  PUT    /v1/sessions/{session_id}/generation
    Queue a generation cycle with no new user message.
    The caller is responsible for any history mutations before calling this
    (e.g. DELETE the last assistant entry to implement regenerate).
    query: ?session_type=dm|group  (default: dm)
    body: { "stream": true }  (optional, default true)
    SSE stream identical to /message, or JSON { "text": "..." }.
    Returns 429 if lane queue is full.

  DELETE /v1/sessions/{session_id}/generation
    Abort the current in-flight generation for this session.
    No-op (204) if no generation is running or session doesn't exist.
    query: ?session_type=dm|group  (default: dm)
    -> 204

  GET    /v1/sessions
    List all sessions — active in-memory lanes AND on-disk session directories.
    query: ?session_type=dm|group  (default: all)
    -> [{ "id", "session_type", "turns", "queue_depth", "queue_max",
          "is_active", "versions": [int, ...] }, ...]

  GET    /v1/sessions/{session_id}/versions
    List version numbers available on disk for a session.
    query: ?session_type=dm|group  (default: dm)
    -> { "id": "...", "versions": [1, 2, 3], "active_version": N }

  DELETE /v1/sessions/{session_id}
    Reset and evict session (clears dialogue, wipes current version JSON).
    -> 204

  PATCH  /v1/sessions/{session_id}/rename
    body: { "new_id": "..." }
    -> { "old_id": "...", "new_id": "..." }

  GET    /v1/sessions/{session_id}/history
    query: ?session_type=dm|group&version=N  (version defaults to active)
    -> [{ "id", "role", "content", "tool_calls", "tool_call_id", "index", "author_id" }, ...]

  PATCH  /v1/sessions/{session_id}/history/{entry_id}
    body: { "content": "..." }
    -> { "updated": true }

  DELETE /v1/sessions/{session_id}/history/{entry_id}
    Deletes exactly that entry (plus dependents per context cascade rules).
    -> { "removed": ["id", ...] }

  POST   /v1/sessions/{session_id}/reset
    Alias for DELETE /v1/sessions/{session_id} — backwards compat.
    -> 204

  GET    /v1/workspace/files/{path}
    -> { "path": "...", "content": "..." }

  PUT    /v1/workspace/files/{path}
    body: { "content": "..." }
    -> { "path": "...", "written": true }

  GET    /v1/health
    -> { "status": "ok", "uptime_s": N, "sessions": { "<key>": {...} } }
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
    Platform, ContentType, content_type_for, ChatType,
    SessionKey, UserIdentity, InboundMessage, Attachment,
    AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError,
)

logger = logging.getLogger(__name__)

_API_AUTHOR = UserIdentity(platform=Platform.API, user_id="api-client", username="api")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_key(session_id: str, session_type: str) -> SessionKey:
    if session_type == "group":
        return SessionKey.group(Platform.API, session_id)
    return SessionKey.dm(session_id)


def _safe_key_str(sk: SessionKey) -> str:
    return str(sk).replace(":", "_")


def _versions_on_disk(sk: SessionKey, sessions_root: Path) -> list[int]:
    d = sessions_root / _safe_key_str(sk)
    if not d.exists():
        return []
    return sorted(int(p.stem) for p in d.glob("*.json") if p.stem.isdigit())


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


def _lane_for(router, key: SessionKey):
    return router._session_router._lanes.get(key)


def _lane_summary(sk: SessionKey, lane, versions: list[int]) -> dict:
    return {
        "id":           sk.conversation_id,
        "session_type": "group" if sk.chat_type == ChatType.GROUP else "dm",
        "turns":        lane.loop._turn_count,
        "queue_depth":  lane.queue.qsize(),
        "queue_max":    lane.queue.maxsize,
        "is_active":    True,
        "versions":     versions,
    }


# ---------------------------------------------------------------------------
# SSE streaming shared helper
# ---------------------------------------------------------------------------

async def _stream_generation(request: web.Request, router, sk: SessionKey) -> web.StreamResponse:
    """
    Register a per-session SSE handler, wait for generation to complete,
    and stream events back to the HTTP client.
    """
    response = web.StreamResponse(headers={
        "Content-Type":      "text/event-stream",
        "Cache-Control":     "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    done_event      = asyncio.Event()
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

    router.register_session_handler(sk, _sse)
    try:
        await done_event.wait()
        await response.write(b'data: {"type": "done"}\n\n')
    finally:
        router.unregister_session_handler(sk)

    await response.write_eof()
    return response


async def _collect_generation(router, sk: SessionKey) -> str:
    """Non-streaming: collect full text from a generation."""
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
        await asyncio.wait_for(done_event.wait(), timeout=120)
    except asyncio.TimeoutError:
        pass
    finally:
        router.unregister_session_handler(sk)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Message handler (POST /v1/sessions/{id}/message)
# ---------------------------------------------------------------------------

async def handle_message(request: web.Request) -> web.StreamResponse:
    router     = request.app["router"]
    session_id = request.match_info["session_id"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "invalid JSON"}))

    text         = body.get("text", "").strip()
    do_stream    = bool(body.get("stream", True))
    session_type = body.get("session_type", "dm")

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
                raise web.HTTPBadRequest(content_type="application/json",
                                         body=json.dumps({"error": f"invalid base64 in '{item.get('name', '?')}'"}))
            parsed.append(Attachment(filename=item.get("name", "file"), data=data,
                                     mime_type=item.get("mime_type", "application/octet-stream")))
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
        accepted = await router.push(msg)
        if not accepted:
            raise web.HTTPTooManyRequests(content_type="application/json",
                                          body=json.dumps({"error": "session queue full"}))
        return await _stream_generation(request, router, sk)
    else:
        accepted = await router.push(msg)
        if not accepted:
            raise web.HTTPTooManyRequests(content_type="application/json",
                                          body=json.dumps({"error": "session queue full"}))
        text_out = await _collect_generation(router, sk)
        return web.Response(content_type="application/json", body=json.dumps({"text": text_out}))


# ---------------------------------------------------------------------------
# Synthetic generation (PUT /v1/sessions/{id}/generation)
# ---------------------------------------------------------------------------

async def handle_generation_put(request: web.Request) -> web.Response:
    """
    Queue a generation cycle against the current context, with no new user
    message. Use after mutating history (e.g. DELETE last assistant entry)
    to trigger a fresh response.
    """
    router     = request.app["router"]
    session_id = request.match_info["session_id"]

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass  # body is optional for this endpoint

    session_type = request.rel_url.query.get("session_type", "dm")
    do_stream    = bool(body.get("stream", True))
    sk           = _session_key(session_id, session_type)

    accepted = await router.push_synthetic(sk)
    if not accepted:
        raise web.HTTPTooManyRequests(content_type="application/json",
                                      body=json.dumps({"error": "session queue full"}))

    if do_stream:
        return await _stream_generation(request, router, sk)
    else:
        text_out = await _collect_generation(router, sk)
        return web.Response(content_type="application/json", body=json.dumps({"text": text_out}))


# ---------------------------------------------------------------------------
# Abort (DELETE /v1/sessions/{id}/generation)
# ---------------------------------------------------------------------------

async def handle_generation_delete(request: web.Request) -> web.Response:
    """Abort the current in-flight generation. No-op if nothing is running."""
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)
    router.abort_generation(sk)  # no-op if lane doesn't exist
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# Session list (GET /v1/sessions)
# ---------------------------------------------------------------------------

async def handle_sessions_list(request: web.Request) -> web.Response:
    router        = request.app["router"]
    sessions_root = request.app["sessions_root"]
    session_type  = request.rel_url.query.get("session_type", None)

    results: dict[str, dict] = {}

    # 1. Active in-memory lanes
    for sk, lane in router._session_router._lanes.items():
        if session_type == "dm"    and sk.chat_type != ChatType.DM:    continue
        if session_type == "group" and sk.chat_type != ChatType.GROUP: continue
        versions = _versions_on_disk(sk, sessions_root)
        results[sk.conversation_id] = _lane_summary(sk, lane, versions)

    # 2. On-disk session directories not currently in memory
    if sessions_root.exists():
        for entry in sessions_root.iterdir():
            if not entry.is_dir():
                continue
            # Directory name format: "dm_<id>" or "group_api_<id>"
            name = entry.name
            if name.startswith("dm_"):
                sid  = name[3:]
                stype = "dm"
                sk   = SessionKey.dm(sid)
            elif name.startswith("group_api_"):
                sid  = name[10:]
                stype = "group"
                sk   = SessionKey.group(Platform.API, sid)
            else:
                continue

            if session_type == "dm"    and stype != "dm":    continue
            if session_type == "group" and stype != "group": continue

            if sid in results:
                continue  # already covered by active lane

            versions = _versions_on_disk(sk, sessions_root)
            if not versions:
                continue

            results[sid] = {
                "id":           sid,
                "session_type": stype,
                "turns":        None,   # unknown — not in memory
                "queue_depth":  0,
                "queue_max":    0,
                "is_active":    False,
                "versions":     versions,
            }

    out = sorted(results.values(), key=lambda x: x["id"])
    return web.Response(content_type="application/json", body=json.dumps(out))


# ---------------------------------------------------------------------------
# Version list (GET /v1/sessions/{id}/versions)
# ---------------------------------------------------------------------------

async def handle_session_versions(request: web.Request) -> web.Response:
    sessions_root = request.app["sessions_root"]
    session_id    = request.match_info["session_id"]
    session_type  = request.rel_url.query.get("session_type", "dm")
    sk            = _session_key(session_id, session_type)

    versions = _versions_on_disk(sk, sessions_root)
    lane     = _lane_for(request.app["router"], sk)
    active_v = lane.loop._session_version if lane else (max(versions) if versions else None)

    return web.Response(
        content_type="application/json",
        body=json.dumps({"id": session_id, "versions": versions, "active_version": active_v}),
    )


# ---------------------------------------------------------------------------
# Session delete / rename
# ---------------------------------------------------------------------------

async def handle_session_delete(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)
    router.reset_session(sk)
    return web.Response(status=204)


async def handle_session_rename(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk_old       = _session_key(session_id, session_type)

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "invalid JSON"}))

    new_id = (body.get("new_id") or "").strip()
    if not new_id:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "new_id required"}))
    if new_id == session_id:
        return web.Response(content_type="application/json",
                            body=json.dumps({"old_id": session_id, "new_id": new_id}))

    sk_new = _session_key(new_id, session_type)

    if hasattr(router, "rename_session"):
        router.rename_session(sk_old, sk_new)
    else:
        logger.warning("Router lacks rename_session — falling back to reset")
        router.reset_session(sk_old)

    return web.Response(content_type="application/json",
                        body=json.dumps({"old_id": session_id, "new_id": new_id}))


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

async def handle_history_get(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    version_q    = request.rel_url.query.get("version")
    sk           = _session_key(session_id, session_type)

    lane = _lane_for(router, sk)

    # If a specific version is requested and it differs from the active one,
    # read directly from disk.
    if version_q is not None:
        try:
            version = int(version_q)
        except ValueError:
            raise web.HTTPBadRequest(content_type="application/json",
                                     body=json.dumps({"error": "version must be an integer"}))
        active_version = lane.loop._session_version if lane else None
        if active_version != version or lane is None:
            sessions_root = request.app["sessions_root"]
            path = sessions_root / _safe_key_str(sk) / f"{version}.json"
            if not path.exists():
                raise web.HTTPNotFound(content_type="application/json",
                                       body=json.dumps({"error": "version not found"}))
            data = json.loads(path.read_text(encoding="utf-8"))
            return web.Response(content_type="application/json",
                                body=json.dumps(data.get("dialogue", [])))

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
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "invalid JSON"}))
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(content_type="application/json",
                                 body=json.dumps({"error": "content required"}))

    lane = _lane_for(router, sk)
    if lane is None:
        raise web.HTTPNotFound(content_type="application/json",
                               body=json.dumps({"error": "session not found"}))
    if not lane.loop.context.edit(entry_id, content):
        raise web.HTTPNotFound(content_type="application/json",
                               body=json.dumps({"error": "entry not found"}))

    return web.Response(content_type="application/json", body=json.dumps({"updated": True}))


async def handle_history_delete(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    entry_id     = request.match_info["entry_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)

    lane = _lane_for(router, sk)
    if lane is None:
        raise web.HTTPNotFound(content_type="application/json",
                               body=json.dumps({"error": "session not found"}))
    removed = lane.loop.context.delete(entry_id)
    if not removed:
        raise web.HTTPNotFound(content_type="application/json",
                               body=json.dumps({"error": "entry not found"}))

    return web.Response(content_type="application/json", body=json.dumps({"removed": removed}))


async def handle_session_reset(request: web.Request) -> web.Response:
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)
    router.reset_session(sk)
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
    payload = {
        "status":   "ok",
        "uptime_s": round(uptime, 1),
        "sessions": {
            str(sk): {
                "id":          sk.conversation_id,
                "turns":       lane.loop._turn_count,
                "queue_depth": lane.queue.qsize(),
                "queue_max":   lane.queue.maxsize,
            }
            for sk, lane in router._session_router._lanes.items()
        },
    }
    return web.Response(content_type="application/json", body=json.dumps(payload))


# ---------------------------------------------------------------------------
# Session next
# ---------------------------------------------------------------------------

async def handle_session_next(request: web.Request) -> web.Response:
    """POST /v1/sessions/{session_id}/next — archive current version, start a fresh one."""
    router       = request.app["router"]
    session_id   = request.match_info["session_id"]
    session_type = request.rel_url.query.get("session_type", "dm")
    sk           = _session_key(session_id, session_type)
    router.next_session(sk)
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# App factory + entrypoint
# ---------------------------------------------------------------------------

def _make_app(router, cfg: GatewayConfig) -> web.Application:
    workspace     = Path(router._config.workspace.path).expanduser().resolve()
    sessions_root = Path("sessions").resolve()

    app = web.Application(middlewares=[_auth_middleware(cfg.api_key)])
    app["router"]        = router
    app["workspace"]     = workspace
    app["sessions_root"] = sessions_root
    app["start_time"]    = time.time()

    # Session management
    app.router.add_get(   "/v1/sessions",                                  handle_sessions_list)
    app.router.add_delete("/v1/sessions/{session_id}",                     handle_session_delete)
    app.router.add_patch( "/v1/sessions/{session_id}/rename",              handle_session_rename)
    app.router.add_get(   "/v1/sessions/{session_id}/versions",            handle_session_versions)

    # Generation
    app.router.add_post(  "/v1/sessions/{session_id}/message",             handle_message)
    app.router.add_put(   "/v1/sessions/{session_id}/generation",          handle_generation_put)
    app.router.add_delete("/v1/sessions/{session_id}/generation",          handle_generation_delete)

    # Session lifecycle
    app.router.add_post(  "/v1/sessions/{session_id}/reset",               handle_session_reset)
    app.router.add_post(  "/v1/sessions/{session_id}/next",                handle_session_next)

    # History
    app.router.add_get(   "/v1/sessions/{session_id}/history",             handle_history_get)
    app.router.add_patch( "/v1/sessions/{session_id}/history/{entry_id}",  handle_history_patch)
    app.router.add_delete("/v1/sessions/{session_id}/history/{entry_id}",  handle_history_delete)

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

"""
sandbox/__main__.py

Minimal HTTP shell-execution service for the TinyCTX sandbox container.

Security model:
  - No auth token. The sandbox port (8700) is only reachable from the agent
    container via the agent_sandbox Docker network (internal: true). Nothing
    else has a route to it. If you can POST /exec, you are the agent.
  - No command policy here. The shell module parses and checks the command
    before dispatching. The sandbox just runs what it receives.
  - Runs as non-root. Root filesystem is read-only. /workspace is the only
    writable surface (shared bind mount with the agent).
  - No LAN / Tailscale access. entrypoint.sh runs as root, installs
    iptables OUTPUT rules in this container's own network namespace, then
    drops to the tinyctx user via su-exec before starting the server.
    Blocks RFC-1918, Tailscale CGNAT, link-local, and loopback.
    The agent_sandbox IPC network (internal: true) has no egress at all.

Environment variables:
  SANDBOX_HOST    — bind host (default 0.0.0.0)
  SANDBOX_PORT    — bind port (default 8700)
  SANDBOX_TIMEOUT — per-command timeout seconds (default 60)
  WORKSPACE_PATH  — path commands run in (default /workspace)
"""
from __future__ import annotations

import json
import asyncio
import logging
import math
import os
import signal
import uuid
from pathlib import Path

try:
    import pwd as _pwd
except ImportError:
    _pwd = None  # type: ignore[assignment]

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sandbox] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def drop_privileges(username: str = "tinyctx") -> None:
    """Drop from root to unprivileged user. No-op if already non-root.
    Requires no-new-privileges:false on the container.
    """
    if os.getuid() != 0:  # type: ignore[attr-defined]
        return
    pw = _pwd.getpwnam(username)  # type: ignore[union-attr]
    os.setgid(pw.pw_gid)  # type: ignore[attr-defined]
    os.setuid(pw.pw_uid)  # type: ignore[attr-defined]
    os.environ["HOME"] = pw.pw_dir
    log.info("privileges dropped to %s (uid=%d)", username, pw.pw_uid)

HOST      = os.environ.get("SANDBOX_HOST", "0.0.0.0")
PORT      = int(os.environ.get("SANDBOX_PORT", "8700"))
TIMEOUT   = int(os.environ.get("SANDBOX_TIMEOUT", "60"))
MAX_OUTPUT_BYTES = int(os.environ.get("SANDBOX_MAX_OUTPUT_BYTES", str(1024 * 1024)))
WORKSPACE = Path(os.environ.get("WORKSPACE_PATH", "/workspace")).resolve()

# Strip everything except what bash needs. No API keys, no tokens.
_SAFE_KEYS = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME")
_ENV = {k: v for k, v in os.environ.items() if k in _SAFE_KEYS}
_ENV.setdefault("HOME", "/tmp")


async def handle_exec(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    command = body.get("command", "").strip()
    if not command:
        return web.Response(status=400, text="missing command")

    request_id = body.get("request_id") or str(uuid.uuid4())
    requested_timeout = body.get("timeout", TIMEOUT)
    if (
        isinstance(requested_timeout, bool)
        or not isinstance(requested_timeout, (int, float))
        or not math.isfinite(requested_timeout)
    ):
        return web.Response(status=400, text="timeout must be a positive number")
    if requested_timeout <= 0:
        return web.Response(status=400, text="timeout must be positive")
    timeout = min(float(requested_timeout), TIMEOUT)

    log.info("exec %s: %.120s (timeout=%.3fs)", request_id, command, timeout)

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            cwd=WORKSPACE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ENV,
            start_new_session=True,
        )

        stdout_task = asyncio.create_task(_drain(process.stdout))
        stderr_task = asyncio.create_task(_drain(process.stderr))
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _terminate_process_group(process)
            await process.wait()
            (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
                stdout_task, stderr_task
            )
            return _json({
                "output": _format_output(stdout, stderr, -1, stdout_truncated, stderr_truncated),
                "exit_code": -1,
                "timed_out": True,
                "request_id": request_id,
            })

        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
            stdout_task, stderr_task
        )
        output = _format_output(
            stdout, stderr, process.returncode, stdout_truncated, stderr_truncated,
        )

        return _json({"output": output, "exit_code": process.returncode, "request_id": request_id})

    except Exception as exc:
        if process is not None:
            _terminate_process_group(process)
            await process.wait()
        return _json({"output": f"[error: {exc}]", "exit_code": -1, "request_id": request_id})


async def _drain(stream) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        remaining = MAX_OUTPUT_BYTES - total
        if remaining > 0:
            chunks.append(chunk[:remaining])
            total += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


def _terminate_process_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _format_output(stdout, stderr, returncode, stdout_truncated, stderr_truncated) -> str:
    parts = []
    if stdout:
        parts.append(stdout.decode("utf-8", "replace").rstrip())
    if stderr:
        parts.append(f"[stderr]\n{stderr.decode('utf-8', 'replace').rstrip()}")
    if stdout_truncated or stderr_truncated:
        parts.append(f"[output truncated at {MAX_OUTPUT_BYTES} bytes per stream]")
    if returncode != 0:
        parts.append(f"[exit {returncode}]")
    return "\n".join(parts) if parts else "[no output]"


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")


def _json(data: dict) -> web.Response:
    return web.Response(status=200, content_type="application/json", text=json.dumps(data))


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/exec",  handle_exec)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    drop_privileges()
    log.info("sandbox  host=%s  port=%d  workspace=%s", HOST, PORT, WORKSPACE)
    web.run_app(build_app(), host=HOST, port=PORT, access_log=None)

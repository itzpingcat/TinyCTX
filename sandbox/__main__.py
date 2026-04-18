"""
sandbox/__main__.py

Minimal HTTP shell-execution service for the TinyCTX sandbox container.

Security model:
  - No auth token. The sandbox port (8700) is only reachable from the agent
    container via the agent_sandbox Docker network (internal: true). Nothing
    else has a route to it. If you can POST /exec, you are the agent.
  - No blacklist here. The shell module enforces the blacklist before
    dispatching. The sandbox just runs what it receives.
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
import logging
import os
import pwd
import subprocess
import sys
from pathlib import Path

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
    if os.getuid() != 0:
        return
    pw = pwd.getpwnam(username)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir
    log.info("privileges dropped to %s (uid=%d)", username, pw.pw_uid)

HOST      = os.environ.get("SANDBOX_HOST", "0.0.0.0")
PORT      = int(os.environ.get("SANDBOX_PORT", "8700"))
TIMEOUT   = int(os.environ.get("SANDBOX_TIMEOUT", "60"))
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

    log.info("exec: %.120s", command)

    try:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            encoding="utf-8",
            errors="replace",
            env=_ENV,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr.rstrip()}")
        if result.returncode != 0:
            parts.append(f"[exit {result.returncode}]")
        output = "\n".join(parts) if parts else "[no output]"

        return _json({"output": output, "exit_code": result.returncode})

    except subprocess.TimeoutExpired:
        return _json({"output": f"[error: timed out after {TIMEOUT}s]", "exit_code": -1})
    except Exception as exc:
        return _json({"output": f"[error: {exc}]", "exit_code": -1})


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

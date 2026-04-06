"""
onboard/gateway_setup.py — Step 4: Gateway port, API key, launch, and health check.

- Prompts for port (validates it's available).
- Auto-generates a gateway API key.
- Writes config, launches the gateway process.
- Health-checks every second for up to 15 seconds.
- On success, launches the CLI bridge and hands off to the agent.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import subprocess
import sys
import time
from typing import Any

import questionary

from .helpers import (
    DEFAULT_GATEWAY_HOST,
    DEFAULT_GATEWAY_PORT,
    REPO_ROOT,
    GoBack,
    Mode,
    QSTYLE,
    c,
    health_ping,
    section,
    success,
    warn,
)

HEALTH_CHECK_TIMEOUT  = 15    # seconds
HEALTH_CHECK_INTERVAL = 1.0   # seconds

# main.py lives at tinyctx/main.py (one level up from commands/)
_MAIN_PY = REPO_ROOT / "tinyctx" / "main.py"


def run(mode: Mode) -> dict[str, Any]:
    """
    Run the gateway setup step.

    Collects port + API key, launches the gateway, health-checks it,
    then hands off to the CLI bridge on success.

    Returns a gateway config dict: { enabled, host, port, api_key }
    Raises GoBack if the user wants to return to the previous step.
    """
    if mode == "quickstart":
        section("Step 4 — Launching Your Agent")
        c.print(
            "TinyCTX runs a local server so clients can connect to your agent.\n"
            "We'll auto-generate a secret key — keep it safe!\n"
        )
        host    = DEFAULT_GATEWAY_HOST
        port    = _pick_port(host)
        api_key = secrets.token_hex(16)
        success(f"Port [bold]{port}[/] is free. API key auto-generated.")
    else:
        section("Step 4 — Gateway (HTTP/SSE API)")
        c.print("Exposes TinyCTX to SillyTavern, curl, and other external clients.\n")

        raw_host = questionary.text(
            "Bind host:",
            default=DEFAULT_GATEWAY_HOST,
            style=QSTYLE,
        ).ask()
        if raw_host is None:
            raise GoBack
        host = raw_host.strip() or DEFAULT_GATEWAY_HOST

        port = _pick_port(host)

        raw_key = questionary.text(
            "API key (leave blank to auto-generate):",
            style=QSTYLE,
        ).ask()
        if raw_key is None:
            raise GoBack
        api_key = raw_key.strip() or secrets.token_hex(16)

        success(f"Gateway: http://{host}:{port}  key=[bold]{api_key}[/]")

    gateway = {
        "enabled": True,
        "host":    host,
        "port":    port,
        "api_key": api_key,
    }

    if not _launch_and_healthcheck(host, port):
        sys.exit(1)

    _launch_cli_bridge(host, port, api_key)

    return gateway


# ── private: launch & health check ───────────────────────────────────────────

def _launch_and_healthcheck(host: str, port: int) -> bool:
    """
    Spawn main.py as a detached background process (mirroring commands/start.py)
    and poll /v1/health every second until healthy or the 15-second timeout
    expires.

    Returns True on success, False on timeout.
    """
    section("Launching Gateway")
    c.print(f"  Starting gateway on http://{host}:{port} …\n")

    log_file = Path.home() / ".tinyctx" / "daemon.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a") as lf:
        if sys.platform == "win32":
            subprocess.Popen(
                [sys.executable, str(_MAIN_PY)],
                cwd=str(REPO_ROOT),
                stdout=lf,
                stderr=lf,
                creationflags=subprocess.DETACHED_PROCESS
                              | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                [sys.executable, str(_MAIN_PY)],
                cwd=str(REPO_ROOT),
                stdout=lf,
                stderr=lf,
                start_new_session=True,
            )

    c.print("  Waiting for gateway", end="", flush=True)
    for _ in range(HEALTH_CHECK_TIMEOUT):
        time.sleep(HEALTH_CHECK_INTERVAL)
        if health_ping(host, port):
            c.print()
            success(
                "Gateway is up! Starting CLI — type your first message to begin.\n"
                f"  Logs: {log_file}"
            )
            return True
        c.print(".", end="", flush=True)

    c.print()
    warn(
        f"Gateway did not respond after {HEALTH_CHECK_TIMEOUT} seconds.\n"
        f"  Check {log_file} for errors.\n"
        "  Please report this issue at: https://github.com/Kawaiineko/TinyCTX/issues"
    )
    return False


def _launch_cli_bridge(host: str, port: int, api_key: str) -> None:
    """Hand off to the CLI bridge via run_detached (blocks until user exits)."""
    from TinyCTX.bridges.cli.__main__ import run_detached

    gateway_url = f"http://{host}:{port}"
    try:
        asyncio.run(run_detached(gateway_url, api_key, {}))
    except KeyboardInterrupt:
        pass


# ── private helpers ───────────────────────────────────────────────────────────

def _is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if the given TCP port is not already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _pick_port(host: str = DEFAULT_GATEWAY_HOST) -> int:
    """Prompt for a port number, validate it's in range and free. Loops until valid."""
    while True:
        raw = questionary.text(
            "Port to listen on:",
            default=str(DEFAULT_GATEWAY_PORT),
            style=QSTYLE,
        ).ask()
        if raw is None:
            raise GoBack
        raw = raw.strip()
        if not raw:
            port = DEFAULT_GATEWAY_PORT
        else:
            try:
                port = int(raw)
            except ValueError:
                warn(f"'{raw}' is not a valid port number. Try again.")
                continue
            if not (1 <= port <= 65535):
                warn("Port must be between 1 and 65535.")
                continue

        if _is_port_available(port, host):
            return port
        warn(f"Port {port} is already in use. Please choose another.")

"""
onboard/gateway_setup.py — Step 4: Gateway port, API key, launch, and health check.

- Prompts for port (validates it's available).
- Auto-generates a gateway API key.
- Returns config dict (does NOT write config or launch — caller does that).
- launch() is called by __main__.py after write_config().
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import subprocess
import sys
import time
from typing import Any
from pathlib import Path
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

# main.py lives at TinyCTX/main.py (REPO_ROOT is the TinyCTX package dir)
_MAIN_PY = REPO_ROOT / "TinyCTX" / "main.py"


def run(mode: Mode) -> dict[str, Any]:
    """
    Collect gateway config (host, port, api_key) from the user.

    Does NOT launch the gateway — call launch() after write_config().

    Returns a gateway config dict: { enabled, host, port, api_key }
    Raises GoBack if the user wants to return to the previous step.
    """
    if mode == "quickstart":
        section("Step 4 — Gateway Setup")
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

        api_key = secrets.token_hex(16)

        success(f"Gateway: http://{host}:{port}  key=[bold]{api_key}[/]")

    return {
        "enabled": True,
        "host":    host,
        "port":    port,
        "api_key": api_key,
    }


def launch(gateway: dict[str, Any]) -> None:
    """
    Spawn main.py as a detached background process and poll /v1/health.
    Call this AFTER write_config() so the daemon finds a valid config on disk.
    """
    host    = gateway["host"]
    port    = gateway["port"]
    api_key = gateway["api_key"]

    if not _launch_and_healthcheck(host, port):
        sys.exit(1)

    _launch_cli_bridge(host, port, api_key)


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

    c.print("  Waiting for gateway", end="")
    for _ in range(HEALTH_CHECK_TIMEOUT):
        time.sleep(HEALTH_CHECK_INTERVAL)
        if health_ping(host, port):
            c.print()
            success(
                "Gateway is up! Starting CLI — type your first message to begin.\n"
                f"  Logs: {log_file}"
            )
            return True
        c.print(".", end="")

    c.print()
    warn(
        f"Gateway did not respond after {HEALTH_CHECK_TIMEOUT} seconds.\n"
        f"  Check {log_file} for errors.\n"
        "  Please report this issue at: https://github.com/itzpingcat/TinyCTX/issues"
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

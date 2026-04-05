"""
onboard/gateway_setup.py — Step 4: Gateway port, API key, launch, and health check.

- Prompts for port (validates it's available).
- Auto-generates a gateway API key.
- Launches the gateway process.
- Health-checks every second for up to 15 seconds.
- On success, launches the CLI bridge and hands off to the agent.
"""

from __future__ import annotations

import secrets
import socket
import sys
import time
from typing import Any

from .helpers import (
    DEFAULT_GATEWAY_HOST,
    DEFAULT_GATEWAY_PORT,
    GoBack,
    Mode,
    c,
    health_ping,
    section,
    success,
    warn,
)

HEALTH_CHECK_TIMEOUT = 15  # seconds
HEALTH_CHECK_INTERVAL = 1  # seconds


def run(mode: Mode) -> dict[str, Any]:
    """
    Run the gateway setup step.

    Returns a gateway config dict:
        { enabled, host, port, api_key }
    Raises GoBack if the user wants to return.
    """
    if mode == "quickstart":
        section("Step 3 — Access Key")
        c.print("TinyCTX uses a local server and a secret key so only you can connect to it.\n")

        raw_key = input("  Gateway API key (Enter to auto-generate, 'back' to go back): ").strip()
        if raw_key.lower() in ("back", "b"):
            raise GoBack
        api_key = raw_key if raw_key else secrets.token_hex(16)
        host = DEFAULT_GATEWAY_HOST
        port = DEFAULT_GATEWAY_PORT

        success(f"Port: [bold]{port}[/]  Key: [bold]{api_key}[/]  (save this somewhere!)")
    else:
        section("Step 4 — Gateway (HTTP/SSE API)")
        c.print("Exposes TinyCTX to SillyTavern, curl, and other external clients.\n")

        raw_host = input(f"  Bind host (default: {DEFAULT_GATEWAY_HOST}, 'back' to go back): ").strip()
        if raw_host.lower() in ("back", "b"):
            raise GoBack
        host = raw_host if raw_host else DEFAULT_GATEWAY_HOST

        port = _pick_port()

        raw_key = input("  API key (Enter to auto-generate): ").strip()
        api_key = raw_key if raw_key else secrets.token_hex(16)

        success(f"Gateway: http://{host}:{port}  key=[bold]{api_key}[/]")

    return {
        "enabled": True,
        "host":    host,
        "port":    port,
        "api_key": api_key,
    }


def launch_and_healthcheck(gateway: dict[str, Any]) -> bool:
    """
    Launch the TinyCTX gateway process and poll until healthy.

    Returns True if the gateway came up within the timeout, False otherwise.
    Prints status as it goes.
    """
    host = gateway["host"]
    port = gateway["port"]

    section("Launching Gateway")
    c.print(f"  Starting gateway on http://{host}:{port} …\n")

    # TODO: replace with your actual gateway launch call
    # e.g. subprocess.Popen([sys.executable, "-m", "main"], ...)
    # For now this is a stub that just health-checks whatever is already running.

    c.print("  Waiting for gateway to become healthy", end="")
    for _ in range(HEALTH_CHECK_TIMEOUT):
        if health_ping(host, port):
            c.print()  # newline after dots
            success("Gateway is healthy!")
            return True
        c.print(".", end="", flush=True)
        time.sleep(HEALTH_CHECK_INTERVAL)

    c.print()
    warn(
        "Gateway did not respond after 15 seconds.\n"
        "  Please report this issue at: https://github.com/your-org/TinyCTX/issues"
    )
    return False


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


def _pick_port() -> int:
    """Prompt for a port number and validate it's free. Loops until valid."""
    while True:
        raw = input(f"  Port (default: {DEFAULT_GATEWAY_PORT}): ").strip()
        if not raw:
            port = DEFAULT_GATEWAY_PORT
        else:
            try:
                port = int(raw)
            except ValueError:
                warn(f"'{raw}' is not a valid port number.")
                continue
            if not (1 <= port <= 65535):
                warn("Port must be between 1 and 65535.")
                continue

        if _is_port_available(port):
            return port
        warn(f"Port {port} is already in use. Please choose another.")

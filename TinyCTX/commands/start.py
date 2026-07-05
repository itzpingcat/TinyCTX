"""
commands/start.py — `tinyctx start`

Starts the TinyCTX stack using Docker Compose and waits until the
gateway responds to /v1/health.

Docker Compose is expected to be run from the project root.

Default config path: <repo_root>/config.yaml

Flags
-----
  --config PATH  Path to config.yaml.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Repo root = TinyCTX/commands/ -> TinyCTX/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "config.yaml"

_POLL_TIMEOUT = 15.0
_POLL_INTERVAL = 0.25


def _health_check(gateway_url: str) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{gateway_url}/v1/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _require_docker() -> None:
    """Ensure Docker and the Docker daemon are available."""

    if shutil.which("docker") is None:
        print(
            "error: Docker is not installed or is not on your PATH.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        print(
            "error: Docker is installed, but the Docker daemon is not running.",
            file=sys.stderr,
        )
        sys.exit(1)


def run(args: argparse.Namespace) -> None:
    config_path = Path(getattr(args, "config", None) or _DEFAULT_CONFIG).resolve()

    if not config_path.exists():
        print("error: no config.yaml found.", file=sys.stderr)
        print(
            "  Run 'tinyctx onboard' to set up TinyCTX first.",
            file=sys.stderr,
        )
        sys.exit(1)

    from TinyCTX.config import load as load_config

    cfg = load_config(str(config_path))
    gateway_url = f"http://{cfg.gateway.host}:{cfg.gateway.port}"
    api_key = cfg.gateway.api_key or ""

    if _health_check(gateway_url):
        print(f"✓ TinyCTX already running — {gateway_url}")
        return

    _require_docker()

    try:
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=_REPO_ROOT,
            check=True,
        )
    except subprocess.CalledProcessError:
        print(
            "error: failed to start TinyCTX with Docker Compose.",
            file=sys.stderr,
        )
        sys.exit(1)

    deadline = time.monotonic() + _POLL_TIMEOUT

    while time.monotonic() < deadline:
        if _health_check(gateway_url):
            print(f"✓ TinyCTX running — {gateway_url}")
            if api_key:
                print(f"  API key: {api_key}")
            return

        time.sleep(_POLL_INTERVAL)

    print(
        f"warning: Docker Compose started, but {gateway_url}/v1/health "
        f"did not respond within {_POLL_TIMEOUT:.0f}s.",
        file=sys.stderr,
    )
    sys.exit(1)

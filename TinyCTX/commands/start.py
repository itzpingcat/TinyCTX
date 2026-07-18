"""
commands/start.py — `tinyctx start`

Starts the TinyCTX stack using Docker Compose and waits until the
gateway responds to /v1/health.

The instance directory (config.yaml, workspace/, data/) is resolved via
commands/_instance.py: --dir, else .tinyctx/ in the current directory,
else ~/.tinyctx. The Docker Compose file itself always lives at the repo
root (shared across all instances) and is invoked with -f/-p plus env
vars pointing at this instance's directories, so multiple instances can
run concurrently without editing compose.yaml.

Flags
-----
  --dir PATH     Path to a .tinyctx instance directory. Overrides
                 autodetection (CWD/.tinyctx, then ~/.tinyctx).
  --config PATH  Path to config.yaml directly. Overrides --dir/autodetect
                 for config loading only (compose still uses --dir/autodetect
                 for workspace/data paths unless --dir is also given).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import os
from pathlib import Path

from TinyCTX.commands._instance import (
    resolve_instance_dir,
    config_path_for,
    project_name_for,
    compose_env,
    load_instance_env,
)

# Repo root = TinyCTX/commands/ -> TinyCTX/ -> repo root.
# compose.yaml lives here, shared across all instances.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_FILE = _REPO_ROOT / "compose.yaml"

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
    instance_dir = resolve_instance_dir(getattr(args, "dir", None))
    config_path = Path(getattr(args, "config", None) or config_path_for(instance_dir)).resolve()

    if not config_path.exists():
        print(f"error: no config.yaml found at {config_path}.", file=sys.stderr)
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

    load_instance_env(instance_dir)

    project_name = project_name_for(instance_dir)
    env = {**os.environ, **compose_env(instance_dir, port=cfg.gateway.port)}

    try:
        subprocess.run(
            ["docker", "compose", "-f", str(_COMPOSE_FILE), "-p", project_name, "up", "-d"],
            cwd=_REPO_ROOT,
            env=env,
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
            print(f"  Instance: {instance_dir}")
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

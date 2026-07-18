"""
commands/stop.py — `tinyctx stop`

Stops the TinyCTX Docker Compose stack for the resolved instance.

Flags
-----
  --dir PATH  Path to a .tinyctx instance directory. Overrides
              autodetection (CWD/.tinyctx, then ~/.tinyctx).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from TinyCTX.commands._instance import resolve_instance_dir, project_name_for, compose_env

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_FILE = _REPO_ROOT / "compose.yaml"


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
    _require_docker()

    instance_dir = resolve_instance_dir(getattr(args, "dir", None))
    project_name = project_name_for(instance_dir)
    env = {**os.environ, **compose_env(instance_dir)}

    try:
        subprocess.run(
            ["docker", "compose", "-f", str(_COMPOSE_FILE), "-p", project_name, "down"],
            cwd=_REPO_ROOT,
            env=env,
            check=True,
        )
    except subprocess.CalledProcessError:
        print(
            "error: failed to stop TinyCTX.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"✓ TinyCTX stopped. ({instance_dir})")

"""
commands/stop.py — `tinyctx stop`

Stops the TinyCTX Docker Compose stack.

Docker Compose is expected to be run from the project root.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


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

    try:
        subprocess.run(
            ["docker", "compose", "down"],
            cwd=_REPO_ROOT,
            check=True,
        )
    except subprocess.CalledProcessError:
        print(
            "error: failed to stop TinyCTX.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("✓ TinyCTX stopped.")

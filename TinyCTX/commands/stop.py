"""
commands/stop.py — `tinyctx stop`

Sends SIGTERM to the daemon and waits up to 5 seconds before SIGKILL.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from TinyCTX.commands import pid as pidfile

_DRAIN_TIMEOUT  = 5.0
_POLL_INTERVAL  = 0.2


def run(args: argparse.Namespace) -> None:
    info = pidfile.read()
    if not info:
        print("TinyCTX is not running (no pid file).")
        return

    daemon_pid = info["pid"]
    if not pidfile.is_alive(daemon_pid):
        print("TinyCTX is not running (stale pid).")
        pidfile.clean()
        return

    print(f"Stopping TinyCTX (pid {daemon_pid})…")
    try:
        if sys.platform == "win32":
            os.kill(daemon_pid, signal.SIGTERM)
        else:
            os.kill(daemon_pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pidfile.clean()
        print("Done.")
        return

    deadline = time.monotonic() + _DRAIN_TIMEOUT
    while time.monotonic() < deadline:
        if not pidfile.is_alive(daemon_pid):
            pidfile.clean()
            print("Done.")
            return
        time.sleep(_POLL_INTERVAL)

    # SIGKILL fallback.
    try:
        if sys.platform == "win32":
            os.kill(daemon_pid, signal.SIGKILL)
        else:
            os.kill(daemon_pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    pidfile.clean()
    print("Force-killed.")

"""
cmd/commands/start.py — `tinyctx start`

Spawns the TinyCTX daemon (main.py) as a detached background process,
writes the PID file, and polls /v1/health until the daemon is ready.

Flags
-----
  --foreground   Run in the foreground instead of detaching.
  --config PATH  Path to config.yaml (default: ./config.yaml).
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
import time
from pathlib import Path

from cmd import pid as pidfile


_POLL_TIMEOUT  = 8.0   # seconds to wait for daemon to come up
_POLL_INTERVAL = 0.25


def _health_check(gateway_url: str) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"{gateway_url}/v1/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def run(args: argparse.Namespace) -> None:
    config_path = Path(getattr(args, "config", None) or "config.yaml").resolve()
    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    # Load config to get gateway URL / api_key.
    sys.path.insert(0, str(config_path.parent))
    from config import load as load_config
    cfg = load_config(str(config_path))
    gateway_url = f"http://{cfg.gateway.host}:{cfg.gateway.port}"
    api_key     = cfg.gateway.api_key or ""

    # Check for an existing live daemon.
    info = pidfile.read()
    if info and pidfile.is_alive(info["pid"]):
        print(f"✓ TinyCTX already running — {info['gateway_url']}")
        print(f"  API key: {info['api_key']}")
        return

    # Clean stale pid.
    if info:
        pidfile.clean()

    log_file = Path.home() / ".tinyctx" / "daemon.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    main_py = config_path.parent / "main.py"

    foreground = getattr(args, "foreground", False)

    if foreground:
        proc = subprocess.Popen(
            [sys.executable, str(main_py)],
            cwd=str(config_path.parent),
        )
    else:
        with open(log_file, "a") as lf:
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    [sys.executable, str(main_py)],
                    cwd=str(config_path.parent),
                    stdout=lf,
                    stderr=lf,
                    creationflags=subprocess.DETACHED_PROCESS
                                  | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = subprocess.Popen(
                    [sys.executable, str(main_py)],
                    cwd=str(config_path.parent),
                    stdout=lf,
                    stderr=lf,
                    start_new_session=True,
                )

    pidfile.write(
        pid=proc.pid,
        gateway_url=gateway_url,
        api_key=api_key,
        config_path=str(config_path),
        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    if foreground:
        proc.wait()
        return

    # Poll health.
    deadline = time.monotonic() + _POLL_TIMEOUT
    while time.monotonic() < deadline:
        if _health_check(gateway_url):
            print(f"✓ TinyCTX running — {gateway_url}")
            print(f"  API key: {api_key}")
            print(f"  logs:    {log_file}")
            return
        time.sleep(_POLL_INTERVAL)

    print("⚠  daemon started but /v1/health not responding within "
          f"{_POLL_TIMEOUT}s. Check {log_file}.")

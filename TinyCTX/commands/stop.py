"""
commands/stop.py — `tinyctx stop`

Asks the running daemon to shut down via POST /v1/shutdown.
Gateway host/port/api_key are read directly from config.yaml.

Default config path: <repo_root>/config.yaml. Override with --config.

Flags
-----
  --config PATH  Path to config.yaml.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

_REPO_ROOT      = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "config.yaml"

_DRAIN_TIMEOUT  = 8.0
_POLL_INTERVAL  = 0.25


def _gateway_url_and_key(args: argparse.Namespace) -> tuple[str, str]:
    config_path = Path(getattr(args, "config", None) or _DEFAULT_CONFIG).resolve()
    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    from TinyCTX.config import load as load_config
    cfg = load_config(str(config_path))
    return f"http://{cfg.gateway.host}:{cfg.gateway.port}", cfg.gateway.api_key or ""


def _is_alive(gateway_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{gateway_url}/v1/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def run(args: argparse.Namespace) -> None:
    gateway_url, api_key = _gateway_url_and_key(args)

    if not _is_alive(gateway_url):
        print("TinyCTX is not running (gateway not reachable).")
        return

    print(f"Stopping TinyCTX at {gateway_url}…")

    req = urllib.request.Request(
        f"{gateway_url}/v1/shutdown",
        method="POST",
        data=b"{}",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            pass
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            pass
        else:
            print(f"error: shutdown request failed: {exc}", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        if not isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)

    deadline = time.monotonic() + _DRAIN_TIMEOUT
    while time.monotonic() < deadline:
        if not _is_alive(gateway_url):
            print("Done.")
            return
        time.sleep(_POLL_INTERVAL)

    print(f"⚠  daemon did not stop within {_DRAIN_TIMEOUT}s — it may still be running.")

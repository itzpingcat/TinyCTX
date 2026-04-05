"""
pid.py — PID file helpers for the TinyCTX daemon.

PID file location: ~/.tinyctx/daemon.pid  (JSON)

Fields stored
-------------
  pid         — OS process id
  gateway_url — e.g. "http://127.0.0.1:8080"
  api_key     — gateway Bearer token
  config_path — absolute path to the config.yaml used to start the daemon
  started_at  — ISO-8601 timestamp string
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_PID_FILE = Path.home() / ".tinyctx" / "daemon.pid"


def _pid_path() -> Path:
    return _PID_FILE


def write(
    pid: int,
    gateway_url: str,
    api_key: str,
    config_path: str,
    started_at: str,
) -> None:
    data = {
        "pid":         pid,
        "gateway_url": gateway_url,
        "api_key":     api_key,
        "config_path": config_path,
        "started_at":  started_at,
    }
    path = _pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read() -> dict | None:
    path = _pid_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def clean() -> None:
    try:
        _pid_path().unlink(missing_ok=True)
    except Exception:
        pass

"""
commands/status.py — `tinyctx status`

Reads gateway host/port/api_key directly from config.yaml and hits
/v1/health to report daemon health. No PID file involved.

Flags
-----
  --config PATH  Path to config.yaml (default: ./config.yaml).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


def _gateway_url_and_key(args: argparse.Namespace) -> tuple[str, str]:
    config_path = Path(getattr(args, "config", None) or "config.yaml").resolve()
    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    from TinyCTX.config import load as load_config
    cfg = load_config(str(config_path))
    return f"http://{cfg.gateway.host}:{cfg.gateway.port}", cfg.gateway.api_key or ""


def _health(gateway_url: str, api_key: str) -> dict | None:
    try:
        req = urllib.request.Request(
            f"{gateway_url}/v1/health",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def run(args: argparse.Namespace) -> None:
    gateway_url, api_key = _gateway_url_and_key(args)

    print(f"Gateway: {gateway_url}")

    health = _health(gateway_url, api_key)
    if not health:
        print("Status:  unreachable")
        return

    if "error" in health:
        print(f"Status:  not running ({health['error']})")
        return

    print(f"Status:  running")
    print(f"Uptime:  {health.get('uptime_s', '?')}s")
    lanes = health.get("lanes", {})
    if lanes:
        print(f"Active lanes: {len(lanes)}")
        for node_id, lane in lanes.items():
            print(f"  {node_id[:8]}…  turns={lane.get('turns')}  "
                  f"queue={lane.get('queue_depth')}/{lane.get('queue_max')}  "
                  f"subscribers={lane.get('subscribers', 0)}")
    else:
        print("Active lanes: 0")

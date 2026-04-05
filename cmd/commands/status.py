"""
cmd/commands/status.py — `tinyctx status`

Reads the PID file and hits /v1/health to report daemon health.
"""
from __future__ import annotations

import argparse
import json
import sys

from cmd import pid as pidfile


def _health(gateway_url: str, api_key: str) -> dict | None:
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{gateway_url}/v1/health",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def run(args: argparse.Namespace) -> None:
    info = pidfile.read()
    if not info:
        print("TinyCTX is not running.")
        return

    daemon_pid = info["pid"]
    alive      = pidfile.is_alive(daemon_pid)

    print(f"PID:         {daemon_pid}  ({'running' if alive else 'dead'})")
    print(f"Gateway:     {info['gateway_url']}")
    print(f"Started:     {info['started_at']}")

    if not alive:
        print("(daemon is dead — run `tinyctx start`)")
        return

    health = _health(info["gateway_url"], info["api_key"])
    if not health:
        print("Health:      unreachable")
        return

    if "error" in health:
        print(f"Health:      error — {health['error']}")
        return

    print(f"Uptime:      {health.get('uptime_s', '?')}s")
    lanes = health.get("lanes", {})
    if lanes:
        print(f"Active lanes: {len(lanes)}")
        for node_id, lane in lanes.items():
            print(f"  {node_id[:8]}…  turns={lane.get('turns')}  "
                  f"queue={lane.get('queue_depth')}/{lane.get('queue_max')}  "
                  f"subscribers={lane.get('subscribers', 0)}")
    else:
        print("Active lanes: 0")

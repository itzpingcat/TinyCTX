"""
commands/launch.py — `tinyctx launch <target>`

Currently supported targets: cli

Reads the PID file for the running daemon, loads bridge options from
config.yaml, and calls the bridge's run_detached() entry point.
"""
from __future__ import annotations

import argparse
import sys

from pathlib import Path

from TinyCTX.utils import pid as pidfile


def run(args: argparse.Namespace) -> None:
    target = getattr(args, "target", "cli")

    if target != "cli":
        print(f"error: unknown launch target '{target}'", file=sys.stderr)
        sys.exit(1)

    info = pidfile.read()

    # Fall back to reading gateway info directly from config if no pid file.
    if not info or not pidfile.is_alive(info["pid"]):
        config_path = str(Path("config.yaml").resolve())
        try:
            from TinyCTX.config import load as load_config
            cfg = load_config(config_path)
            gateway_url = f"http://{cfg.gateway.host}:{cfg.gateway.port}"
            api_key     = cfg.gateway.api_key or ""
        except Exception as exc:
            print(f"error: no running daemon and could not load config: {exc}", file=sys.stderr)
            sys.exit(1)

        # Verify gateway is actually reachable.
        import urllib.request
        try:
            with urllib.request.urlopen(f"{gateway_url}/v1/health", timeout=2) as r:
                if r.status != 200:
                    raise OSError(f"status {r.status}")
        except Exception as exc:
            print(f"error: gateway at {gateway_url} is not responding: {exc}", file=sys.stderr)
            sys.exit(1)

        options: dict = {}
        try:
            bridge_cfg = cfg.bridges.get("cli")
            if bridge_cfg:
                options = getattr(bridge_cfg, "options", {}) or {}
        except Exception:
            pass
    else:
        gateway_url = info["gateway_url"]
        api_key     = info["api_key"]
        config_path = info.get("config_path")

        options: dict = {}
        if config_path:
            try:
                from TinyCTX.config import load as load_config
                cfg = load_config(config_path)
                bridge_cfg = cfg.bridges.get("cli")
                if bridge_cfg:
                    options = getattr(bridge_cfg, "options", {}) or {}
            except Exception:
                pass

    import asyncio
    from TinyCTX.bridges.cli.__main__ import run_detached
    try:
        asyncio.run(run_detached(gateway_url, api_key, options))
    except KeyboardInterrupt:
        pass

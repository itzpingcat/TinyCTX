"""
commands/launch.py — `tinyctx launch <target>`

Currently supported targets: cli

Reads the PID file for the running daemon, loads bridge options from
config.yaml, and calls the bridge's run_detached() entry point.
"""
from __future__ import annotations

import argparse
import sys

from TinyCTX.utils import pid as pidfile


def run(args: argparse.Namespace) -> None:
    target = getattr(args, "target", "cli")

    if target != "cli":
        print(f"error: unknown launch target '{target}'", file=sys.stderr)
        sys.exit(1)

    info = pidfile.read()
    if not info:
        print("error: TinyCTX is not running. Start it first with `tinyctx start`.",
              file=sys.stderr)
        sys.exit(1)

    if not pidfile.is_alive(info["pid"]):
        print("error: daemon pid is stale. Run `tinyctx start` to restart.",
              file=sys.stderr)
        sys.exit(1)

    gateway_url = info["gateway_url"]
    api_key     = info["api_key"]
    config_path = info.get("config_path")

    # Load bridge options from config.yaml.
    options: dict = {}
    if config_path:
        try:
            import sys as _sys
            from pathlib import Path
            _sys.path.insert(0, str(Path(config_path).parent))
            from TinyCTX.config import load as load_config
            cfg = load_config(config_path)
            bridge_cfg = cfg.bridges.get("cli")
            if bridge_cfg:
                options = getattr(bridge_cfg, "options", {}) or {}
        except Exception:
            pass  # options are optional

    import asyncio
    from TinyCTX.bridges.cli.__main__ import run_detached
    asyncio.run(run_detached(gateway_url, api_key, options))

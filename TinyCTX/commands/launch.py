"""
commands/launch.py — `tinyctx launch <target>`

Currently supported targets: cli

Reads gateway host/port/api_key directly from config.yaml and calls
the bridge's run_detached() entry point.

Default config path: <repo_root>/config.yaml. Override with --config.

Flags
-----
  --config PATH  Path to config.yaml.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

_REPO_ROOT      = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "config.yaml"


def run(args: argparse.Namespace) -> None:
    target = getattr(args, "target", "cli")

    if target != "cli":
        print(f"error: unknown launch target '{target}'", file=sys.stderr)
        sys.exit(1)

    config_path = Path(getattr(args, "config", None) or _DEFAULT_CONFIG).resolve()
    if not config_path.exists():
        print("error: no config.yaml found.", file=sys.stderr)
        print("  Run 'TinyCTX onboard' to set up TinyCTX, or manually create a config.yaml.", file=sys.stderr)
        sys.exit(1)

    from TinyCTX.config import load as load_config
    try:
        cfg = load_config(str(config_path))
    except Exception as exc:
        print(f"error: could not load config: {exc}", file=sys.stderr)
        sys.exit(1)

    gateway_url = f"http://{cfg.gateway.host}:{cfg.gateway.port}"
    api_key     = cfg.gateway.api_key or ""

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

    import asyncio
    from TinyCTX.bridges.cli.__main__ import run_detached
    try:
        asyncio.run(run_detached(gateway_url, api_key, options))
    except KeyboardInterrupt:
        pass

"""
commands/launch.py — `tinyctx launch <target>`

Currently supported targets: cli

Reads gateway host/port/api_key directly from config.yaml and calls
the bridge's run_detached() entry point.

Default config path: resolved instance directory's config.yaml
(see utils/instance.py). Override with --dir or --config.

Flags
-----
  --dir PATH       Path to a .tinyctx instance directory.
  --config PATH    Path to config.yaml directly (overrides --dir/autodetect).
  --user USERNAME  TinyCTX username to log in as. If the user's
                   permission_level is below 100, you will be prompted
                   to elevate it (CLI is a trusted admin console — no
                   higher-level caller is required).

Docker
------
When TinyCTX is running inside a container, attach to the container and
run this command from within it:

    docker exec -it <container_name> python -m TinyCTX launch cli --user USERNAME

Or, if you have the TinyCTX CLI installed on the host and the gateway
port is published (e.g. -p 8085:8085), just run:

    tinyctx launch cli --user USERNAME

and point it at the published port — no docker exec needed because the
CLI bridge connects to the gateway over HTTP, not a Unix socket.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from TinyCTX.utils.instance import resolve_instance_dir, config_path_for


def _prompt_elevate(username: str, current_level: int) -> bool:
    """Ask the user if they want to elevate to level 100. Returns True if yes."""
    print(
        f"\n  User '{username}' has permission_level {current_level}.\n"
        "  The CLI is a trusted admin console — you can elevate this user to\n"
        "  level 100 now. This grants full access to all agent capabilities.\n"
    )
    while True:
        try:
            answer = input("  Elevate to level 100? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("  Please enter y or n.")


def run(args: argparse.Namespace) -> None:
    target = getattr(args, "target", "cli")

    if target != "cli":
        print(f"error: unknown launch target '{target}'", file=sys.stderr)
        sys.exit(1)

    instance_dir = resolve_instance_dir(getattr(args, "dir", None))
    config_path = Path(getattr(args, "config", None) or config_path_for(instance_dir)).resolve()
    if not config_path.exists():
        print(f"error: no config.yaml found at {config_path}.", file=sys.stderr)
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

    # ── Resolve user ──────────────────────────────────────────────────────────
    # User lookup and elevation go through the gateway so we always hit the
    # correct users.db (e.g. the one inside Docker), not a local copy.
    username: str | None = getattr(args, "user", None)

    if username is None:
        try:
            username = input("  TinyCTX username: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)

    if not username:
        print("error: username cannot be empty.", file=sys.stderr)
        sys.exit(1)

    auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # Fetch user from gateway.
    try:
        req = urllib.request.Request(
            f"{gateway_url}/v1/user/{username}",
            headers=auth_headers,
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            user_data = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"error: user '{username}' not found in users.db.", file=sys.stderr)
            print("  Check the username with: python -m TinyCTX.onboard.fix_permissions --user <name> --list", file=sys.stderr)
        else:
            print(f"error: gateway returned {exc.code} looking up user.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"error: could not reach gateway to look up user: {exc}", file=sys.stderr)
        sys.exit(1)

    permission_level = user_data["permission_level"]

    # ── Offer elevation if level < 100 ────────────────────────────────────────
    if permission_level < 100:
        if _prompt_elevate(username, permission_level):
            try:
                payload = json.dumps({"permission_level": 100}).encode()
                req = urllib.request.Request(
                    f"{gateway_url}/v1/user/{username}/elevate",
                    data=payload,
                    headers={**auth_headers, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    json.loads(r.read().decode())  # confirm success
                print(f"  \u2713 '{username}' elevated to level 100.\n")
            except Exception as exc:
                print(f"  warning: elevation failed: {exc}", file=sys.stderr)
        else:
            print(f"  Continuing as level {permission_level}.\n")

    # ── Launch CLI ────────────────────────────────────────────────────────────
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
        asyncio.run(run_detached(gateway_url, api_key, options, username=username, instance_dir=instance_dir))
    except KeyboardInterrupt:
        pass

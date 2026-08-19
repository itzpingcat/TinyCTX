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
  --user USERNAME  TinyCTX username to log in as. If it doesn't exist yet,
                   you will be prompted to create it (starts on the single
                   configured permissions.template, no overrides). If the
                   user isn't already elevated, you will also be prompted to
                   grant full admin access (CLI is a trusted admin console —
                   no ROOT-holding caller is required; physical/API-key
                   access is the authorization).

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


def _prompt_create(username: str) -> bool:
    """Ask the user if they want to create a new TinyCTX user. Returns True if yes."""
    try:
        answer = input(f"  User '{username}' not found. Create it? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _prompt_elevate(username: str) -> bool:
    """Ask the user if they want to grant full admin access. Returns True if yes."""
    print(
        f"\n  User '{username}' is not currently elevated.\n"
        "  The CLI is a trusted admin console — you can grant this user\n"
        "  every agent capability now.\n"
    )
    while True:
        try:
            answer = input("  Grant full admin access? [y/N] ").strip().lower()
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
            if not _prompt_create(username):
                print(f"error: user '{username}' not found in users.db.", file=sys.stderr)
                print("  Check the username with: python -m TinyCTX.onboard.fix_permissions --user <name> --list", file=sys.stderr)
                sys.exit(1)
            try:
                req = urllib.request.Request(
                    f"{gateway_url}/v1/user/{username}",
                    data=b"{}",
                    headers={**auth_headers, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    user_data = json.loads(r.read().decode())
                print(f"  ✓ created user '{username}'.\n")
            except Exception as create_exc:
                print(f"error: could not create user '{username}': {create_exc}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"error: gateway returned {exc.code} looking up user.", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"error: could not reach gateway to look up user: {exc}", file=sys.stderr)
        sys.exit(1)

    is_admin = bool(user_data.get("admin"))

    # ── Offer elevation if not already an admin ─────────────────────────────────
    if not is_admin:
        if _prompt_elevate(username):
            try:
                payload = json.dumps({}).encode()
                req = urllib.request.Request(
                    f"{gateway_url}/v1/user/{username}/elevate",
                    data=payload,
                    headers={**auth_headers, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    json.loads(r.read().decode())  # confirm success
                print(f"  \u2713 '{username}' elevated — every permission granted.\n")
            except Exception as exc:
                print(f"  warning: elevation failed: {exc}", file=sys.stderr)
        else:
            print(f"  Continuing without admin access.\n")

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

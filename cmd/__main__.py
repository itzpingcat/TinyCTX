"""
cmd/__main__.py — `tinyctx` CLI entrypoint.

Usage
-----
  tinyctx onboard          Setup wizard
  tinyctx start            Start the gateway daemon
  tinyctx stop             Stop the daemon
  tinyctx status           Show daemon health
  tinyctx launch cli       Attach interactive CLI to running daemon
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tinyctx",
        description="TinyCTX — agent framework CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # onboard
    p_onboard = sub.add_parser("onboard", help="Run the setup wizard")
    p_onboard.add_argument("--reset", action="store_true",
                           help="Re-run onboarding from scratch")

    # start
    p_start = sub.add_parser("start", help="Start the gateway daemon")
    p_start.add_argument("--foreground", action="store_true",
                         help="Run in the foreground (don't detach)")
    p_start.add_argument("--config", metavar="PATH",
                         help="Path to config.yaml")

    # stop
    sub.add_parser("stop", help="Stop the gateway daemon")

    # status
    sub.add_parser("status", help="Show daemon health")

    # launch
    p_launch = sub.add_parser("launch", help="Launch a bridge client")
    p_launch.add_argument("target", nargs="?", default="cli",
                          help="Bridge to launch (default: cli)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "onboard":
        from cmd.commands.onboard import run
        run(args)

    elif args.command == "start":
        from cmd.commands.start import run
        run(args)

    elif args.command == "stop":
        from cmd.commands.stop import run
        run(args)

    elif args.command == "status":
        from cmd.commands.status import run
        run(args)

    elif args.command == "launch":
        from cmd.commands.launch import run
        run(args)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

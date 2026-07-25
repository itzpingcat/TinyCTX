"""
__main__.py — `tinyctx` CLI entrypoint.

Usage
-----
  tinyctx onboard          Setup wizard
  tinyctx start [-w]       Start the gateway daemon (-w streams docker logs)
  tinyctx stop             Stop the daemon
  tinyctx restart [-w]     Restart the daemon (-w streams docker logs)
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
    p_onboard.add_argument("--dir", metavar="PATH",
                           help="Path to a .tinyctx instance directory to set up (default: ~/.tinyctx)")

    # start
    p_start = sub.add_parser("start", help="Start the gateway daemon")
    p_start.add_argument("--foreground", action="store_true",
                         help="Run in the foreground (don't detach)")
    p_start.add_argument("--dir", metavar="PATH",
                         help="Path to a .tinyctx instance directory")
    p_start.add_argument("--config", metavar="PATH",
                         help="Path to config.yaml")
    p_start.add_argument("-w", "--watch", action="store_true",
                         help="Stream docker logs after starting (Ctrl+C stops streaming, not the daemon)")

    # stop
    p_stop = sub.add_parser("stop", help="Stop the gateway daemon")
    p_stop.add_argument("--dir", metavar="PATH",
                        help="Path to a .tinyctx instance directory")

    # restart
    p_restart = sub.add_parser("restart", help="Restart the gateway daemon")
    p_restart.add_argument("--dir", metavar="PATH",
                           help="Path to a .tinyctx instance directory")
    p_restart.add_argument("--config", metavar="PATH",
                           help="Path to config.yaml")
    p_restart.add_argument("-w", "--watch", action="store_true",
                           help="Stream docker logs after restarting (Ctrl+C stops streaming, not the daemon)")

    # status
    p_status = sub.add_parser("status", help="Show daemon health")
    p_status.add_argument("--dir", metavar="PATH",
                          help="Path to a .tinyctx instance directory")
    p_status.add_argument("--config", metavar="PATH",
                          help="Path to config.yaml")

    # launch
    p_launch = sub.add_parser("launch", help="Launch a bridge client")
    p_launch.add_argument("target", nargs="?", default="cli",
                          help="Bridge to launch (default: cli)")
    p_launch.add_argument("--user", metavar="USERNAME",
                          help="TinyCTX username to log in as")
    p_launch.add_argument("--dir", metavar="PATH",
                          help="Path to a .tinyctx instance directory")
    p_launch.add_argument("--config", metavar="PATH",
                          help="Path to config.yaml")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "onboard":
        from TinyCTX.commands.onboard import run
        run(args)

    elif args.command == "start":
        from TinyCTX.commands.start import run
        run(args)

    elif args.command == "stop":
        from TinyCTX.commands.stop import run
        run(args)

    elif args.command == "restart":
        from TinyCTX.commands.restart import run
        run(args)

    elif args.command == "status":
        from TinyCTX.commands.status import run
        run(args)

    elif args.command == "launch":
        from TinyCTX.commands.launch import run
        run(args)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

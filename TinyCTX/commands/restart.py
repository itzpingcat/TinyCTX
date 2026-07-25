"""
commands/restart.py — `tinyctx restart`

Stops then starts the TinyCTX stack (wrapper around stop + start).

Flags
-----
  --dir PATH     Path to a .tinyctx instance directory. Overrides
                 autodetection (CWD/.tinyctx, then ~/.tinyctx).
  --config PATH  Path to config.yaml directly. Overrides --dir/autodetect
                 for config loading only.
  -w, --watch    Stream `docker compose logs -f` after restarting. Ctrl+C
                 stops the log stream only — the daemon keeps running.
"""

from __future__ import annotations

import argparse

from TinyCTX.commands import start as start_cmd
from TinyCTX.commands import stop as stop_cmd


def run(args: argparse.Namespace) -> None:
    stop_cmd.run(args)
    start_cmd.run(args)

"""
commands/onboard.py — `tinyctx onboard`

Thin wrapper around the onboard package.
"""
from __future__ import annotations

import argparse
import sys


def run(args: argparse.Namespace) -> None:
    sys.argv = ["onboard"] + (["--reset"] if getattr(args, "reset", False) else [])
    from TinyCTX.onboard.__main__ import main as _onboard_main
    _onboard_main()

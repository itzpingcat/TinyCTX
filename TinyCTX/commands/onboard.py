"""
commands/onboard.py — `tinyctx onboard`

Thin wrapper around the onboard package.

Resolves the target instance directory the same way every other command
does (utils/instance.py: --dir, else .tinyctx/ in CWD, else
~/.tinyctx) and hands it to the onboard wizard via TINYCTX_INSTANCE_DIR,
since onboard/helpers.py needs it before argparse runs inside
onboard.__main__.main().
"""
from __future__ import annotations

import argparse
import os
import sys

from TinyCTX.utils.instance import resolve_instance_dir


def run(args: argparse.Namespace) -> None:
    instance_dir = resolve_instance_dir(getattr(args, "dir", None))
    os.environ["TINYCTX_INSTANCE_DIR"] = str(instance_dir)

    sys.argv = ["onboard"] + (["--reset"] if getattr(args, "reset", False) else [])
    from TinyCTX.onboard.__main__ import main as _onboard_main
    _onboard_main()

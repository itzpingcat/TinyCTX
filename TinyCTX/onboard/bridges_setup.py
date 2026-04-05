"""
onboard/bridges_setup.py — Step 2: Bridge selection and configuration.

Each bridge has its own setup file in onboard/bridges/<name>.py.
This module presents a checkbox-style selection and then calls the
appropriate per-bridge setup.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import questionary

from .helpers import GoBack, Mode, QSTYLE, c, section, success, warn

# Bridges that are bundled with TinyCTX.
# Each name here must have a matching file at onboard/bridges/<name>.py
AVAILABLE_BRIDGES = [
    "discord",
    "matrix",
    "telegram",
]

BRIDGES_DIR = Path(__file__).parent / "bridges"


def run(mode: Mode) -> dict[str, Any]:
    """
    Present a checkbox-style bridge picker and run each chosen bridge's
    setup module.

    Returns a bridges config dict (always includes cli: enabled).
    Raises GoBack if the user wants to return to the previous step.
    """
    if mode == "quickstart":
        section("Step 2 — Connect to Platforms (optional)")
        c.print("TinyCTX can run as a bot on Discord, Matrix, or Telegram.")
        c.print("You can skip this and configure bridges later.\n")
    else:
        section("Step 2 — Bridges")
        c.print("CLI is always enabled. Select additional bridges to configure.\n")

    # Build choices list — only show bridges that have a setup file
    available = [b for b in AVAILABLE_BRIDGES if (BRIDGES_DIR / f"{b}.py").exists()]
    if not available:
        warn("No bridge setup files found in onboard/bridges/ — skipping.")
        return {"cli": {"enabled": True}}

    choices = [
        questionary.Choice(title=b.title(), value=b)
        for b in available
    ]
    choices.append(questionary.Choice(title="← Skip bridges for now", value="__skip__"))

    raw = questionary.checkbox(
        "Which bridges would you like to configure?",
        choices=choices,
        style=QSTYLE,
    ).ask()

    if raw is None:
        raise GoBack

    chosen = [b for b in raw if b != "__skip__"]

    bridges: dict[str, Any] = {"cli": {"enabled": True}}

    for bridge_name in chosen:
        module_path = f"onboard.bridges.{bridge_name}"
        try:
            mod = importlib.import_module(module_path)
            bridge_cfg = mod.run(mode)
            if bridge_cfg:
                bridges[bridge_name] = bridge_cfg
            success(f"{bridge_name.title()} bridge configured.")
        except ImportError:
            warn(f"No setup file found for bridge '{bridge_name}' — skipping.")
        except GoBack:
            warn(f"Skipped {bridge_name.title()} bridge setup.")
        except Exception as e:
            warn(f"Error setting up {bridge_name.title()} bridge: {e}")

    if not chosen:
        c.print("  No bridges selected — CLI only. You can add bridges later by re-running onboard.\n")

    return bridges

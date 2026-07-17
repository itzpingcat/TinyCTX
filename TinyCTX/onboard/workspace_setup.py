"""
onboard/workspace_setup.py — Step 3: Workspace bootstrapping.

- Workspace path is fixed at <instance>/workspace (see helpers.INSTANCE_DIR).
- Unpacks BOOTSTRAP.md if the workspace is empty or brand-new.
- Quietly copies boilerplate files (AGENTS.md, SOUL.md) if missing.
- Quietly installs the bundled cron skill if missing.
- Offers optional recommended skills via a checkbox.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import questionary

from .helpers import (
    BUNDLED_DIR,
    DEFAULT_WORKSPACE,
    Mode,
    QSTYLE,
    c,
    section,
    success,
    warn,
)

# Boilerplate .md files that live in onboard/bundled/
BOILERPLATE_MD = ["AGENTS.md", "SOUL.md", "TOOLS.md", "USER.md"]

# Optional recommended skills (must be zip files in onboard/bundled/)
RECOMMENDED_SKILLS = ["clawhub", "weather", "skill-creator"]


def run(mode: Mode) -> str:
    """
    Run the workspace setup step.

    Workspace path is fixed at <instance>/workspace (no longer prompted for) —
    workspace, data, and config.yaml all live under one instance directory
    now, so relocating the workspace independently no longer makes sense.
    To use a different instance entirely, run `tinyctx onboard --dir PATH`.

    Returns the workspace path string.
    """
    if mode == "quickstart":
        section("Step 2 — Where to Save Your Data")
        c.print(f"TinyCTX will store your sessions and memory here.\n")
        c.print(f"  Workspace: [bold]{DEFAULT_WORKSPACE}[/]\n")
    else:
        section("Step 3 — Workspace")
        c.print("Stores sessions, memory index, SOUL.md, AGENTS.md, skills, etc.\n")
        c.print(f"  Workspace: [bold]{DEFAULT_WORKSPACE}[/]\n")

    workspace = DEFAULT_WORKSPACE
    ws_path = Path(workspace).expanduser()
    ws_path.mkdir(parents=True, exist_ok=True)

    _bootstrap(ws_path)
    _boilerplate(ws_path)
    _cron_skill(ws_path)
    _optional_skills(ws_path, mode)

    success(f"Workspace: [bold]{ws_path}[/]")
    return workspace


# ── private helpers ───────────────────────────────────────────────────────────

def _bootstrap(ws_path: Path) -> None:
    """Unpack BOOTSTRAP.md if the workspace is completely empty."""
    bootstrap_src = BUNDLED_DIR / "BOOTSTRAP.md"
    if not bootstrap_src.exists():
        return
    if any(ws_path.iterdir()):
        return  # workspace already has content
    dest = ws_path / "BOOTSTRAP.md"
    try:
        shutil.copy2(bootstrap_src, dest)
        success("Unpacked BOOTSTRAP.md into fresh workspace.")
    except Exception as e:
        warn(f"Could not copy BOOTSTRAP.md: {e}")


def _boilerplate(ws_path: Path) -> None:
    """Quietly copy boilerplate .md files if they don't already exist."""
    for fname in BOILERPLATE_MD:
        src  = BUNDLED_DIR / fname
        dest = ws_path / fname
        if not src.exists() or dest.exists():
            continue
        try:
            shutil.copy2(src, dest)
            # Quiet — no success() message per the PLAN
        except Exception as e:
            warn(f"Could not copy {fname}: {e}")


def _cron_skill(ws_path: Path) -> None:
    """Quietly install the bundled cron skill if not already present."""
    cron_src = BUNDLED_DIR / "skills" / "cron"
    if not cron_src.exists():
        return
    skills_dir = ws_path / "skills"
    skills_dir.mkdir(exist_ok=True)
    cron_dest = skills_dir / "cron"
    if cron_dest.exists():
        return
    try:
        shutil.copytree(cron_src, cron_dest)
        # Quiet — no success() message per the PLAN
    except Exception as e:
        warn(f"Could not install cron skill: {e}")


def _optional_skills(ws_path: Path, mode: Mode) -> None:
    """Offer optional recommended skills via a checkbox."""
    available = [s for s in RECOMMENDED_SKILLS if (BUNDLED_DIR / f"{s}.zip").exists()]
    if not available:
        return

    skills_dir = ws_path / "skills"
    skills_dir.mkdir(exist_ok=True)

    # Filter to skills not already installed
    to_offer = [s for s in available if not (skills_dir / s).exists()]
    if not to_offer:
        return

    if mode == "quickstart":
        c.print("\nWould you like to install any recommended skills?\n")
    else:
        c.print("\n  Optional recommended skills available:\n")

    choices = [questionary.Choice(title=s, value=s) for s in to_offer]
    chosen = questionary.checkbox(
        "Select skills to install (space to select, enter to confirm):",
        choices=choices,
        style=QSTYLE,
    ).ask()

    if not chosen:
        return

    for skill_name in chosen:
        zip_path = BUNDLED_DIR / f"{skill_name}.zip"
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(skills_dir)
            success(f"Installed [bold]{skill_name}[/] skill.")
        except Exception as e:
            warn(f"Could not install {skill_name}: {e}")

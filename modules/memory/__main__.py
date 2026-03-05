"""
modules/memory/__main__.py

Registers prompt providers for SOUL.md, AGENTS.md, and MEMORY.md.
Each file is read fresh on every assemble() — edit in place, no restart needed.
Missing files are silently skipped (return None = no injection).

The agent edits these files via the normal filesystem tools (str_replace, create_file, etc).
This module owns nothing except reading and injecting.

Convention: register(agent_loop) — no imports from utils or contracts.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def register(agent_loop) -> None:
    workspace = Path(agent_loop.config.memory.workspace_path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        from modules.memory import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    def resolve(filename: str) -> Path:
        p = Path(filename)
        return p if p.is_absolute() else workspace / p

    soul_path   = resolve(cfg.get("soul_file",   "SOUL.md"))
    agents_path = resolve(cfg.get("agents_file", "AGENTS.md"))
    memory_path = resolve(cfg.get("memory_file", "MEMORY.md"))

    def _read(path: Path) -> str | None:
        """Read a file, return None if missing or empty."""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
            return text or None
        except Exception as exc:
            logger.warning("[memory] could not read %s: %s", path, exc)
            return None

    agent_loop.context.register_prompt(
        "soul",
        lambda _ctx: _read(soul_path),
        role="system",
        priority=int(cfg.get("soul_priority", 0)),
    )
    agent_loop.context.register_prompt(
        "agents",
        lambda _ctx: _read(agents_path),
        role="system",
        priority=int(cfg.get("agents_priority", 10)),
    )
    agent_loop.context.register_prompt(
        "memory",
        lambda _ctx: _read(memory_path),
        role="system",
        priority=int(cfg.get("memory_priority", 20)),
    )

    logger.info(
        "[memory] registered providers — soul: %s | agents: %s | memory: %s",
        soul_path, agents_path, memory_path,
    )
"""
main.py — Application entrypoint.
Scans bridges/ for packages, checks config.bridges.<name>.enabled,
starts each enabled bridge. No bridge names hardcoded.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path

from config import load as load_config, apply_logging
from gateway import Gateway

logger = logging.getLogger(__name__)

BRIDGES_DIR = Path("bridges")


async def main() -> None:
    cfg = load_config()
    apply_logging(cfg.logging)

    gw = Gateway(config=cfg)

    tasks: list[asyncio.Task] = []

    for entry in sorted(BRIDGES_DIR.iterdir()):
        if not entry.is_dir() or not (entry / "__main__.py").exists():
            continue

        name = entry.name

        # Check config.bridges.<name>.enabled — skip if no config entry or disabled
        bridge_cfg = getattr(cfg.bridges, name, None)
        if bridge_cfg is None:
            logger.debug("Bridge '%s' has no config entry — skipping", name)
            continue
        if not bridge_cfg.enabled:
            logger.debug("Bridge '%s' is disabled — skipping", name)
            continue

        try:
            mod = importlib.import_module(f"bridges.{name}.__main__")
            if not hasattr(mod, "run"):
                logger.warning("Bridge '%s' has no run() — skipping", name)
                continue
            tasks.append(asyncio.create_task(mod.run(gw), name=f"bridge:{name}"))
            logger.info("Started bridge '%s'", name)
        except Exception:
            logger.exception("Failed to load bridge '%s'", name)

    if not tasks:
        logger.error("No bridges started — nothing to do. Check config.yaml bridges section.")
        return

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
        try: await t
        except asyncio.CancelledError: pass

    await gw.shutdown()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
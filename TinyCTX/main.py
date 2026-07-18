"""
main.py — Application entrypoint.

Startup order:
  1. Load config, init gateway.
  2. Start server (if config.server.enabled) — runs as a peer task.
  3. Scan bridges/, start each enabled bridge as a task.
  4. Wait for any task to complete (normal exit or crash).
  5. Cancel remaining tasks, shutdown gateway.

The server and bridges are peers — either can trigger shutdown if they exit.
The server is started before bridges so API clients can connect as soon as
the process is up, even before bridge tasks are running.

Bridges that set MANUAL_LAUNCH = True at module level are skipped — they
are launched on demand via `tinyctx launch <bridge>`.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
from pathlib import Path

from TinyCTX.config import load as load_config, apply_logging, resolve_log_level
from TinyCTX.contracts import MANUAL_LAUNCH_ATTR
from TinyCTX.runtime import Runtime
from TinyCTX.ai import configure_parallel

logger = logging.getLogger(__name__)

BRIDGES_DIR = Path(__file__).parent / "bridges"
GATEWAY_MOD = "TinyCTX.gateway.__main__"


def _startup_log_level(cfg) -> int:
    return resolve_log_level(cfg.logging.level, default=logging.INFO)


async def main() -> None:
    logger.debug("loading config")
    cfg = load_config(os.environ.get("TINYCTX_CONFIG_FILE") or "config.yaml")
    apply_logging(cfg.logging, level_override=_startup_log_level(cfg))
    logger.debug("gateway.enabled=%s bridges=%s", cfg.gateway.enabled, list(cfg.bridges))

    configure_parallel(cfg.parallel)
    logger.debug("ai.py request queue parallel=%d", cfg.parallel)

    logger.debug("creating runtime")
    gw = Runtime(config=cfg)
    logger.debug("runtime created")
    await gw.start()

    tasks: list[asyncio.Task] = []
    bridge_modules: dict[asyncio.Task, object] = {}

    # ------------------------------------------------------------------ gateway
    if cfg.gateway.enabled:
        logger.debug("starting gateway")
        if not cfg.gateway.api_key:
            logger.warning("gateway.api_key is empty — gateway is unauthenticated!")
        try:
            logger.debug("importing gateway module")
            gateway_mod = importlib.import_module(GATEWAY_MOD)
            logger.debug("gateway module imported")
            tasks.append(asyncio.create_task(
                gateway_mod.run(gw, cfg.gateway),
                name="gateway",
            ))
            logger.debug("gateway task created")
            logger.info(
                "Started gateway on %s:%d",
                cfg.gateway.host, cfg.gateway.port,
            )
        except Exception:
            logger.exception("Failed to start gateway")

    # ------------------------------------------------------------------ bridges
    logger.debug("scanning bridges")
    if BRIDGES_DIR.exists():
        for entry in sorted(BRIDGES_DIR.iterdir()):
            if not entry.is_dir() or not (entry / "__main__.py").exists():
                continue

            name = entry.name
            logger.debug("found bridge '%s'", name)
            bridge_cfg = cfg.bridges.get(name)
            if bridge_cfg is None:
                logger.debug("Bridge '%s' has no config entry — skipping", name)
                continue
            if not bridge_cfg.enabled:
                logger.debug("Bridge '%s' is disabled — skipping", name)
                continue

            try:
                mod = importlib.import_module(f"TinyCTX.bridges.{name}.__main__")
                if getattr(mod, MANUAL_LAUNCH_ATTR, False):
                    logger.debug("Bridge '%s' is manual-launch only — skipping auto-start", name)
                    continue
                if not hasattr(mod, "run"):
                    logger.warning("Bridge '%s' has no run() — skipping", name)
                    continue
                bridge_task = asyncio.create_task(mod.run(gw), name=f"bridge:{name}")
                tasks.append(bridge_task)
                bridge_modules[bridge_task] = mod
                logger.info("Started bridge '%s'", name)
            except Exception:
                logger.exception("Failed to load bridge '%s'", name)

    logger.debug("%d tasks created, entering wait", len(tasks))
    if not tasks:
        logger.error("Nothing started — enable at least one bridge or the server in config.yaml.")
        return

    # ------------------------------------------------------------------ run
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for t in done:
        if t.exception():
            logger.error("Task '%s' crashed: %s", t.get_name(), t.exception())
        else:
            logger.info("Task '%s' exited cleanly.", t.get_name())

    for t in pending:
        mod = bridge_modules.get(t)
        close_fn = getattr(mod, "close", None) if mod else None
        if close_fn is not None:
            try:
                logger.info("Closing task '%s' via module close() hook", t.get_name())
                await asyncio.wait_for(close_fn(gw), timeout=5)
            except Exception:
                logger.exception("Task '%s' close() hook failed", t.get_name())

        t.cancel()
        try:
            await asyncio.wait_for(t, timeout=5)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.warning(
                "Task '%s' did not finish within 5s of cancellation — abandoning it.",
                t.get_name(),
            )

    await gw.shutdown()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    import signal 

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    shutdown_event = asyncio.Event()

    def _request_shutdown():
        logger.info("Shutdown signal received — draining tasks…")
        loop.call_soon_threadsafe(shutdown_event.set)

    # SIGINT (Ctrl-C) and SIGTERM both trigger graceful drain.
    # Windows doesn't support SIGTERM via add_signal_handler, so we fall back
    # to a KeyboardInterrupt catch at the run level.
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown)
    except NotImplementedError:
        pass  # Windows — KeyboardInterrupt covers SIGINT

    async def _run_with_shutdown():
        main_task = loop.create_task(main())
        await asyncio.wait(
            [main_task, loop.create_task(shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not main_task.done():
            logger.info("Cancelling all tasks for graceful shutdown…")
            for t in asyncio.all_tasks(loop):
                if t is not asyncio.current_task():
                    t.cancel()
            # Give tasks a moment to finish their finally blocks
            await asyncio.sleep(0.5)

    try:
        loop.run_until_complete(_run_with_shutdown())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        logger.info("Event loop closed.")

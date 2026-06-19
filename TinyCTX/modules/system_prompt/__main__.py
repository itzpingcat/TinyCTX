"""
modules/system_prompt/__main__.py

Static system-prompt injection: SOUL.md, AGENTS.md, TOOLS.md.

This module's only job is to register the four file-backed prompt providers
onto the agent cycle's context.  The RAG pipeline (indexing, hybrid search,
memory_search tool, consolidation hook) lives in modules/rag/__main__.py.

Both modules must be loaded for the full memory system to work:

    modules:
      - system_prompt
      - rag
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# register_runtime — nothing to do for the prompt-only side
# ---------------------------------------------------------------------------

def register_runtime(runtime) -> None:
    pass


# ---------------------------------------------------------------------------
# register_agent — static prompt providers
# ---------------------------------------------------------------------------

def register_agent(cycle) -> None:
    try:
        from TinyCTX.modules.system_prompt import EXTENSION_META
        defaults: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        defaults = {}

    overrides: dict = {}
    if hasattr(cycle.config, "extra") and isinstance(cycle.config.extra, dict):
        overrides = cycle.config.extra.get("memory_search", {})

    cfg = {**defaults, **overrides}

    workspace = Path(cycle.config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    def _resolve(filename: str) -> Path:
        p = Path(filename)
        return p if p.is_absolute() else workspace / p

    from TinyCTX.modules.system_prompt.inject import MacroResolver, make_provider
    resolver = MacroResolver()

    for key, cfg_key, priority_key in (
        ("soul",   "soul_file",   "soul_priority"),
        ("agents", "agents_file", "agents_priority"),
        # ("memory", "memory_file", "memory_priority"),
        ("tools",  "tools_file",  "tools_priority"),
    ):
        path = _resolve(cfg[cfg_key])
        cycle.context.register_prompt(
            key,
            make_provider(path, workspace, extra_macros=resolver),
            role="system",
            priority=int(cfg[priority_key]),
        )
        logger.debug("[system_prompt] registered prompt '%s' from %s", key, path)

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

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base. Nested dicts merge
    key-by-key (so e.g. overriding config.soul.priority doesn't drop
    config.soul.file); any other value type is replaced outright."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def register_agent(cycle) -> None:
    try:
        from TinyCTX.modules.system_prompt import EXTENSION_META
        defaults: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        defaults = {}

    overrides: dict = {}
    if hasattr(cycle.config, "extra") and isinstance(cycle.config.extra, dict):
        overrides = cycle.config.extra.get("system_prompt", {})

    cfg = _deep_merge(defaults, overrides)

    workspace = Path(cycle.config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    def _resolve(filename: str) -> Path:
        p = Path(filename)
        return p if p.is_absolute() else workspace / p

    from TinyCTX.modules.system_prompt.inject import MacroResolver, make_provider
    resolver = MacroResolver()

    for key in ("soul", "agents", "tools"):
        # ("memory", ...) intentionally omitted — see commented-out entry below.
        section = cfg.get(key, {})
        path = _resolve(section["file"])
        cycle.context.register_prompt(
            key,
            make_provider(path, workspace, extra_macros=resolver),
            role="system",
            priority=int(section["priority"]),
        )
        logger.debug("[system_prompt] registered prompt '%s' from %s", key, path)

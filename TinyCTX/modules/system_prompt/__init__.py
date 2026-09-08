"""
modules/system_prompt

Static system-prompt injection: SOUL.md, AGENTS.md, TOOLS.md as file-backed
prompt providers. The RAG pipeline (indexing, hybrid search, memory_search
tool, consolidation hook) lives in modules/rag. Both must be loaded for the
full memory system to work:

    modules:
      - system_prompt
      - rag
"""
from __future__ import annotations

import logging
from pathlib import Path

from TinyCTX.decorators import hook, prompt
from TinyCTX.hooks import HookType
from TinyCTX.module import Module

from .inject import MacroResolver, make_provider

logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base. Nested dicts merge
    key-by-key (so e.g. overriding soul.priority doesn't drop soul.file);
    any other value type is replaced outright."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SystemPrompt(Module):
    """Injects workspace markdown files (SOUL.md, AGENTS.md, TOOLS.md) as
    system prompt providers."""

    settings = {
        "soul":   {"default": {"file": "SOUL.md",   "priority": 0},
                   "type": "dict", "description": "Injected first (lowest priority)."},
        "agents": {"default": {"file": "AGENTS.md", "priority": 10},
                   "type": "dict", "description": "Injected after soul."},
        "tools":  {"default": {"file": "TOOLS.md",  "priority": 15},
                   "type": "dict", "description": "Injected last."},
        # ("memory", ...) intentionally omitted — memory's own prompt lives
        # in modules/memory, not here.
    }

    def resolve_settings(self, extra):
        # Overridden (rather than the base's whole-value replace) because
        # each setting here is itself a {file, priority} dict — a caller
        # overriding just one subkey (e.g. soul.priority) must not silently
        # drop the other (soul.file), which resolve_settings()'s per-top-
        # level-key replacement would do.
        resolved = {key: spec.get("default") for key, spec in self.settings.items()}
        overrides = extra.get(self.name, {}) if extra and isinstance(extra, dict) else {}
        for key in resolved:
            if key in overrides:
                if isinstance(resolved[key], dict) and isinstance(overrides[key], dict):
                    resolved[key] = _deep_merge(resolved[key], overrides[key])
                else:
                    resolved[key] = overrides[key]
        return resolved

    @hook(HookType.STARTUP)
    def load(self, runtime) -> None:
        workspace = Path(runtime.config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self._workspace = workspace

        resolver = MacroResolver()

        def _resolve(filename: str) -> Path:
            p = Path(filename)
            return p if p.is_absolute() else workspace / p

        self._soul_provider   = make_provider(_resolve(self.config["soul"]["file"]),   workspace, extra_macros=resolver)
        self._agents_provider = make_provider(_resolve(self.config["agents"]["file"]), workspace, extra_macros=resolver)
        self._tools_provider  = make_provider(_resolve(self.config["tools"]["file"]),  workspace, extra_macros=resolver)
        logger.debug("[system_prompt] providers ready (soul/agents/tools)")

    # @prompt's priority is fixed at class-definition time (decorators run
    # before any instance/config exists), so a configured soul/agents/tools
    # priority override no longer moves these relative to each other — same
    # narrowing MODULES-PLAN-P1.md's own equipment_manifest example accepts
    # for prompt_priority. The hardcoded values below match this module's
    # own shipped defaults.
    @prompt(role="system", priority=0, name="soul")
    def soul(self, ctx) -> str | None:
        return self._soul_provider(ctx)

    @prompt(role="system", priority=10, name="agents")
    def agents(self, ctx) -> str | None:
        return self._agents_provider(ctx)

    @prompt(role="system", priority=15, name="tools")
    def tools(self, ctx) -> str | None:
        return self._tools_provider(ctx)

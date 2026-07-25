"""
modules/memory/extractor.py

Extractor librarian: reads unvisited conversation branches and ingests entities
and relationships into the graph, within a resolved write scope.

Scope resolution = environment state + humans who spoke in the last N messages
(scopes.resolve_scopes). New nodes default to `global`; the extractor narrows
scope only for sensitive/personal info, and can only write to scopes within the
set it was handed.
"""
from __future__ import annotations

import logging
from pathlib import Path

from TinyCTX.modules.memory import scopes as _scopes
from TinyCTX.modules.memory import tools as _tools
from TinyCTX.modules.memory.librarian_common import agent_loop, make_tool_handler

logger = logging.getLogger(__name__)
_PROMPTS = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


async def run_extractor(cfg, conn, write_lock, llm, batch_text, agent_name,
                        scope_set: set, agent_logger) -> None:
    """Ingest a batch of conversation text into the graph under scope_set."""
    vocab = await _tools.relation_vocab()
    system = _prompt("extractor_system.txt").format(
        relation_vocab=vocab,
        agent_name=agent_name,
        writable_scopes=", ".join(sorted(scope_set)),
    )
    user = _prompt("extractor_user.txt").format(batch_text=batch_text)
    with _tools.scope_context(scope_set):
        await agent_loop(llm, system, user, make_tool_handler(), agent_logger)


def resolve_extractor_scopes(env: dict, node_authors: set[str]) -> set[str]:
    """Write scope set for an extraction: global + guild + the participants."""
    return _scopes.writable_scopes(_scopes.resolve_scopes(env, node_authors))

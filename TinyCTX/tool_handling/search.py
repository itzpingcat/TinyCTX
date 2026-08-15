"""
tool_handling/search.py

Shared ranking logic for tool discovery — used by both:
  - the passive, automatic per-turn pass (ToolCallHandler.passive_search),
    which silently auto-enables top hits before the model sees its tool list;
  - the explicit, model-invoked `tools_search` tool (ToolCallHandler.tools_search),
    which only lists candidates and requires a follow-up call to enable one.

Both read the same ToolVectorStore corpus (one embedding per tool, synced
fresh each turn since the tool registry itself is rebuilt every AgentCycle —
see sync_store below) and both fuse BM25 + vector via reciprocal-rank fusion
(TinyCTX.utils.rrf.rrf_fuse — the one fusion implementation shared across
tool discovery, memory search, and RAG search): RRF is scale-invariant
(no min-max normalization step) and was already proven in the memory module.
"""
from __future__ import annotations

import logging
from typing import Any

from TinyCTX.utils.bm25 import BM25
from TinyCTX.utils.rrf import rrf_fuse as _rrf_fuse
from TinyCTX.tool_handling.vector_store import ToolVectorStore, content_hash

logger = logging.getLogger(__name__)


def _embed_text_for(name: str, description: str) -> str:
    """Canonical string embedded/hashed for a tool — same shape tools_search's
    existing BM25 corpus already uses (name words + description), so BM25 and
    vector rank the same text."""
    return f"{name.replace('_', ' ')} {description}"


async def sync_store(store: ToolVectorStore, tools: dict[str, Any], embedder, embedding_model: str) -> None:
    """
    Bring `store` up to date with the current turn's tool registry.

    The registry (`tools`, ToolCallHandler.tools) is rebuilt fresh every
    AgentCycle, but embeddings are cached by content hash — a tool whose
    name/description hasn't changed since last turn costs zero embed calls.
    Only genuinely new or changed tools get (re)embedded; stale rows for
    tools no longer registered (module unloaded, config change) are dropped.

    If embedder is None (no embedding_model configured), rows are still kept
    in sync for BM25 (text/hash written, embedding left NULL) so vector
    search can be turned on later without a cold start.
    """
    current_names = set(tools.keys())
    removed = store.remove_stale(current_names)
    if removed:
        logger.debug("[tool_handling/search] removed %d stale tool row(s): %s", len(removed), removed)

    dirty: list[tuple[str, str, str]] = []  # (name, text, text_hash)
    for name, tool in tools.items():
        text = _embed_text_for(name, tool.get("description", ""))
        text_hash = content_hash(text)
        if store.is_dirty(name, text_hash, embedding_model):
            dirty.append((name, text, text_hash))

    if not dirty:
        store.commit()
        return

    embeddings: list[list[float] | None] = [None] * len(dirty)
    if embedder is not None:
        try:
            embeddings = await embedder.embed(
                [text for _, text, _ in dirty], priority=15, kind="document"
            )
        except Exception as exc:
            logger.warning(
                "[tool_handling/search] embedder unreachable syncing %d tool(s) — "
                "BM25 only this turn: %s", len(dirty), exc,
            )
            embeddings = [None] * len(dirty)

    for (name, text, text_hash), emb in zip(dirty, embeddings):
        store.upsert(name, text, text_hash, embedding_model if emb is not None else "", emb)
    store.commit()
    logger.debug("[tool_handling/search] synced %d tool row(s) (%s)", len(dirty),
                 "with vectors" if embedder is not None else "BM25 only, no embedder")


async def rank_tools(
    query: str,
    tools: dict[str, Any],
    store: ToolVectorStore,
    *,
    embedder=None,
    embedding_model: str = "",
    vector_enabled: bool = False,
    top_k: int = 5,
    min_score: float = 0.0,
    rrf_w: float = 0.4,
    rrf_k: int = 60,
) -> list[str]:
    """
    Rank registered tool names against `query`. Always does BM25; also fuses
    in vector similarity (RRF) when vector_enabled and embedder is usable.
    Falls back silently to BM25-only on any embed failure — callers never
    need to branch on whether vector search actually ran.

    Returns up to `top_k` tool names, descending relevance, filtered to
    fused score >= min_score. Does not enable or mutate anything — pure
    ranking; callers (passive_search / tools_search) decide what to do with
    the result.
    """
    if not tools:
        return []

    # -- BM25 --
    bm25_ranks: dict[str, int] = {}
    corpus = {name: _embed_text_for(name, tool.get("description", "")) for name, tool in tools.items()}
    bm25 = BM25(corpus)
    hits = bm25.search(query, top_k=len(corpus))
    for rank, (name, score) in enumerate((h for h in hits if h[1] > 0.0), start=1):
        bm25_ranks[name] = rank

    # -- vector (optional) --
    vec_ranks: dict[str, int] = {}
    if vector_enabled and embedder is not None:
        try:
            qvec = (await embedder.embed([query], priority=5, kind="query"))[0]
            if qvec is not None:
                hits = store.vector_search(qvec, limit=len(tools))
                for rank, (name, _score) in enumerate(hits, start=1):
                    if name in tools:  # store may lag a beat behind sync_store on races
                        vec_ranks[name] = rank
        except Exception as exc:
            logger.warning("[tool_handling/search] vector rank failed: %s -- BM25 only", exc)

    if not bm25_ranks and not vec_ranks:
        return []

    fused = _rrf_fuse(bm25_ranks, vec_ranks, bm25_w=rrf_w, rrf_k=rrf_k)
    return [name for name, score in fused if score >= min_score][:top_k]

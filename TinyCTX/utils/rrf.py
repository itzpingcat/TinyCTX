"""
utils/rrf.py

Single implementation of reciprocal-rank fusion (RRF), used everywhere the
project fuses a BM25 ranking and a vector-similarity ranking into one score:
modules/memory/tools.py (entity search), tool_handling/search.py (tool
discovery), and modules/rag/store.py (databank search).

RRF was chosen over min-max-normalize-then-linearly-blend because it's
scale-invariant — BM25 and cosine scores live on different, unbounded-vs-
bounded scales, and RRF sidesteps that by fusing on rank position instead of
raw score.

Usage
-----
    bm25_ranks = {"a": 1, "b": 2}   # 1-based rank, best = 1
    vec_ranks  = {"b": 1, "c": 3}
    fused = rrf_fuse(bm25_ranks, vec_ranks)
    # → [("b", ...), ("a", ...), ("c", ...)]  descending by fused score
"""
from __future__ import annotations


def rrf_fuse(
    bm25_ranks: dict, vec_ranks: dict, *, bm25_w: float = 0.4, rrf_k: int = 60
) -> list[tuple]:
    """Reciprocal-rank fusion over two {key: rank} maps (rank is 1-based,
    best = 1). `bm25_w` is BM25's weight; the vector weight is `1 - bm25_w`.

    Returns [(key, score), ...] descending by fused score.
    """
    vec_w = 1.0 - bm25_w
    scores: dict = {}
    for key, rank in bm25_ranks.items():
        scores[key] = scores.get(key, 0.0) + bm25_w / (rrf_k + rank)
    for key, rank in vec_ranks.items():
        scores[key] = scores.get(key, 0.0) + vec_w / (rrf_k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)

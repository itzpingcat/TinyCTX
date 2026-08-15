"""
Tests for the shared RRF fusion helper (utils/rrf.py), consolidated out of
modules/memory/tools.py and tool_handling/search.py, and now also used by
modules/rag/store.py's hybrid_search.
"""
from __future__ import annotations

from TinyCTX.utils.rrf import rrf_fuse


def test_rrf_fuse_prefers_dual_hits():
    fused = dict(rrf_fuse({"a": 1, "b": 2}, {"b": 1, "c": 3}))
    assert max(fused, key=fused.get) == "b"  # ranked in both retrievers


def test_rrf_fuse_bm25_only():
    fused = dict(rrf_fuse({"a": 1, "b": 2}, {}))
    assert set(fused) == {"a", "b"}
    assert fused["a"] > fused["b"]  # rank 1 beats rank 2


def test_rrf_fuse_vector_only():
    fused = dict(rrf_fuse({}, {"a": 1, "b": 2}))
    assert set(fused) == {"a", "b"}
    assert fused["a"] > fused["b"]


def test_rrf_fuse_empty():
    assert rrf_fuse({}, {}) == []


def test_rrf_fuse_weight_shifts_ranking():
    # "a" wins BM25 rank 1, loses vector rank 3; "b" is the reverse.
    bm25_ranks = {"a": 1, "b": 3}
    vec_ranks  = {"a": 3, "b": 1}
    bm25_heavy = dict(rrf_fuse(bm25_ranks, vec_ranks, bm25_w=0.9))
    vec_heavy  = dict(rrf_fuse(bm25_ranks, vec_ranks, bm25_w=0.1))
    assert bm25_heavy["a"] > bm25_heavy["b"]
    assert vec_heavy["b"] > vec_heavy["a"]

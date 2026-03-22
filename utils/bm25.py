"""
utils/bm25.py

Lightweight in-memory BM25 for small corpora (tool registries, etc.).

No dependencies beyond stdlib. Zero I/O — operates entirely over a
dict[str, str] mapping document IDs to their text content.

BM25 parameters
---------------
k1  — term frequency saturation (default 1.5)
      Higher → TF contributes more before saturating.
b   — length normalisation (default 0.75)
      1.0 = full normalisation, 0.0 = no length penalty.

Usage
-----
    corpus = {"shell": "run shell commands", "view": "read a file with line numbers"}
    bm25   = BM25(corpus)
    hits   = bm25.search("read file", top_k=5)
    # → [("view", 0.93), ("shell", 0.0)]
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

def _tokenise(text: str) -> List[str]:
    """
    Lowercase, split on non-alphanumeric boundaries, drop empty tokens.
    Underscore-separated names (e.g. 'web_search') are split into constituent
    words so 'search' matches 'web_search' without needing the full name.
    """
    text = text.lower().replace("_", " ").replace("-", " ")
    return [t for t in re.split(r"[^a-z0-9]+", text) if t]


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

class BM25:
    """
    Okapi BM25 over an in-memory corpus.

    Build once per query session (construction is O(N·L) where N = number of
    documents and L = average document length). For a tool registry of ~50
    entries this is essentially free.
    """

    def __init__(
        self,
        corpus: Dict[str, str],
        k1: float = 1.5,
        b:  float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b  = b

        # Tokenise all documents
        self._ids:    List[str]       = list(corpus.keys())
        self._tokens: List[List[str]] = [_tokenise(v) for v in corpus.values()]

        doc_count   = len(self._ids)
        lengths     = [len(t) for t in self._tokens]
        self._avgdl = sum(lengths) / doc_count if doc_count else 1.0

        # Per-document term frequencies and corpus-wide document frequencies
        self._tf: List[Counter]   = [Counter(t) for t in self._tokens]
        self._df: Dict[str, int]  = {}
        for tf in self._tf:
            for term in tf:
                self._df[term] = self._df.get(term, 0) + 1

        self._N = doc_count

    # ------------------------------------------------------------------
    # IDF
    # ------------------------------------------------------------------

    def _idf(self, term: str) -> float:
        """Robertson-Sparck Jones IDF with +0.5 smoothing (never negative)."""
        df = self._df.get(term, 0)
        return math.log((self._N - df + 0.5) / (df + 0.5) + 1.0)

    # ------------------------------------------------------------------
    # Score one document for a set of query terms
    # ------------------------------------------------------------------

    def _score(self, doc_idx: int, query_terms: List[str]) -> float:
        tf_doc = self._tf[doc_idx]
        dl     = sum(tf_doc.values())
        score  = 0.0
        for term in query_terms:
            if term not in tf_doc:
                continue
            tf  = tf_doc[term]
            idf = self._idf(term)
            numerator   = tf * (self.k1 + 1.0)
            denominator = tf + self.k1 * (1.0 - self.b + self.b * dl / self._avgdl)
            score += idf * numerator / denominator
        return score

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """
        Return up to top_k (doc_id, score) pairs sorted by descending score.
        Documents with score == 0.0 are excluded unless top_k is larger than
        the number of matching documents.
        """
        terms = _tokenise(query)
        if not terms:
            return []

        scores = [
            (self._ids[i], self._score(i, terms))
            for i in range(self._N)
        ]
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

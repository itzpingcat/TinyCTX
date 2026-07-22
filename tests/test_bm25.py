"""
tests/test_bm25.py

Tests for utils/bm25.py — lightweight in-memory Okapi BM25 keyword search.

Run with:
    pytest tests/
"""
from __future__ import annotations

from TinyCTX.utils.bm25 import BM25, _tokenise


class TestTokenise:
    def test_lowercases(self):
        assert _tokenise("Hello World") == ["hello", "world"]

    def test_splits_on_non_alphanumeric(self):
        assert _tokenise("read, a file!") == ["read", "a", "file"]

    def test_splits_underscore_names(self):
        assert _tokenise("web_search") == ["web", "search"]

    def test_splits_hyphenated_names(self):
        assert _tokenise("well-known") == ["well", "known"]

    def test_empty_string(self):
        assert _tokenise("") == []

    def test_only_punctuation(self):
        assert _tokenise("!!!") == []

    def test_numbers_kept(self):
        assert _tokenise("gpt4 turbo") == ["gpt4", "turbo"]


class TestBM25Ranking:
    def test_matching_doc_ranks_above_nonmatching(self):
        corpus = {
            "view": "read a file with line numbers",
            "shell": "run shell commands",
        }
        bm25 = BM25(corpus)
        hits = bm25.search("read file")
        ids = [doc_id for doc_id, _ in hits]
        assert ids[0] == "view"

    def test_matching_doc_has_positive_score(self):
        corpus = {
            "view": "read a file with line numbers",
            "shell": "run shell commands",
        }
        bm25 = BM25(corpus)
        hits = dict(bm25.search("read file"))
        assert hits["view"] > 0.0

    def test_nonmatching_doc_has_zero_score(self):
        corpus = {
            "view": "read a file with line numbers",
            "shell": "run shell commands",
        }
        bm25 = BM25(corpus)
        hits = dict(bm25.search("read file"))
        assert hits["shell"] == 0.0

    def test_more_occurrences_of_rare_term_ranks_higher(self):
        corpus = {
            "many": "xylophone xylophone xylophone practice room",
            "one": "xylophone practice room only",
            "none": "practice room only music",
        }
        bm25 = BM25(corpus)
        hits = dict(bm25.search("xylophone"))
        assert hits["many"] > hits["one"] > hits["none"] == 0.0

    def test_multi_term_query_sums_contributions(self):
        corpus = {
            "both": "apple banana",
            "one_only": "apple cherry",
            "neither": "date fig",
        }
        bm25 = BM25(corpus)
        hits = dict(bm25.search("apple banana"))
        assert hits["both"] > hits["one_only"] > hits["neither"] == 0.0

    def test_results_sorted_descending(self):
        corpus = {
            "a": "cat cat cat",
            "b": "cat dog",
            "c": "dog dog dog",
        }
        bm25 = BM25(corpus)
        hits = bm25.search("cat")
        scores = [score for _, score in hits]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_limits_results(self):
        corpus = {
            "a": "cat",
            "b": "cat cat",
            "c": "cat cat cat",
            "d": "dog",
        }
        bm25 = BM25(corpus)
        hits = bm25.search("cat", top_k=2)
        assert len(hits) == 2

    def test_top_k_can_include_zero_score_docs(self):
        """The implementation sorts and slices the full scored list, so a
        top_k larger than the match count still returns zero-score docs."""
        corpus = {"a": "cat", "b": "dog"}
        bm25 = BM25(corpus)
        hits = bm25.search("cat", top_k=10)
        assert len(hits) == 2
        assert dict(hits)["b"] == 0.0


class TestBM25EdgeCases:
    def test_empty_corpus(self):
        bm25 = BM25({})
        assert bm25.search("anything") == []

    def test_empty_query_returns_empty_list(self):
        corpus = {"a": "some text here"}
        bm25 = BM25(corpus)
        assert bm25.search("") == []

    def test_whitespace_only_query_returns_empty_list(self):
        corpus = {"a": "some text here"}
        bm25 = BM25(corpus)
        assert bm25.search("   ") == []

    def test_punctuation_only_query_returns_empty_list(self):
        corpus = {"a": "some text here"}
        bm25 = BM25(corpus)
        assert bm25.search("???") == []

    def test_query_with_no_matches_returns_all_zero_scores(self):
        corpus = {"a": "apple", "b": "banana"}
        bm25 = BM25(corpus)
        hits = bm25.search("zzz")
        assert all(score == 0.0 for _, score in hits)

    def test_query_case_insensitive_match(self):
        corpus = {"a": "Read A File"}
        bm25 = BM25(corpus)
        hits = dict(bm25.search("READ file"))
        assert hits["a"] > 0.0

    def test_single_document_corpus(self):
        corpus = {"only": "hello world"}
        bm25 = BM25(corpus)
        hits = bm25.search("hello")
        assert hits[0][0] == "only"
        assert hits[0][1] > 0.0

    def test_default_top_k_is_ten(self):
        corpus = {f"doc{i}": "cat" for i in range(15)}
        bm25 = BM25(corpus)
        hits = bm25.search("cat")
        assert len(hits) == 10


class TestBM25Parameters:
    def test_higher_k1_increases_saturated_term_contribution(self):
        # A document with many repeats of the query term should score higher
        # relative to idf-only baseline when k1 is larger (less saturation).
        corpus = {"rep": "word " * 20 + "filler"}
        low_k1 = BM25(corpus, k1=0.1)
        high_k1 = BM25(corpus, k1=5.0)
        low_score = dict(low_k1.search("word"))["rep"]
        high_score = dict(high_k1.search("word"))["rep"]
        assert high_score > low_score

    def test_b_zero_removes_length_normalisation(self):
        corpus = {
            "short": "cat",
            "long": "cat " + "filler " * 50,
        }
        bm25_b0 = BM25(corpus, b=0.0)
        hits = dict(bm25_b0.search("cat"))
        # With no length normalisation, tf=1 for both docs and idf equal,
        # so scores should be identical regardless of document length.
        assert hits["short"] == hits["long"]

    def test_b_one_penalises_longer_documents(self):
        corpus = {
            "short": "cat",
            "long": "cat " + "filler " * 50,
        }
        bm25_b1 = BM25(corpus, b=1.0)
        hits = dict(bm25_b1.search("cat"))
        assert hits["short"] > hits["long"]

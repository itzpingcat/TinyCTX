"""
tests/test_sanitize.py

Tests for utils/sanitize.py — Unicode bracket homoglyph sanitization.

Run with:
    pytest tests/
"""
from __future__ import annotations

from TinyCTX.utils.sanitize import sanitize_brackets


class TestSanitizeBrackets:
    def test_empty_string(self):
        assert sanitize_brackets("") == ""

    def test_plain_ascii_unchanged(self):
        assert sanitize_brackets("hello (world) [1]") == "hello (world) [1]"

    def test_fullwidth_parens(self):
        assert sanitize_brackets("（x）") == "(x)"

    def test_fullwidth_brackets(self):
        assert sanitize_brackets("［x］") == "[x]"

    def test_cjk_lenticular_brackets_used_as_label_delimiters(self):
        """These are the delimiters context.py uses for 【author】: labels —
        the whole point of this sanitizer is stopping user text from spoofing them."""
        assert sanitize_brackets("【kamie】") == "[kamie]"

    def test_angle_brackets_variants(self):
        assert sanitize_brackets("⟨x⟩") == "<x>"
        assert sanitize_brackets("〈x〉") == "<x>"

    def test_curly_brace_variants(self):
        assert sanitize_brackets("｛x｝") == "{x}"

    def test_mixed_content(self):
        text = "normal text 【tag】 more text"
        assert sanitize_brackets(text) == "normal text [tag] more text"

    def test_non_bracket_unicode_unaffected(self):
        assert sanitize_brackets("héllo wörld 日本語") == "héllo wörld 日本語"

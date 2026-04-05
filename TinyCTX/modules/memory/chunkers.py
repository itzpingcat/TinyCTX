"""
modules/memory/chunkers.py

Pluggable chunking strategies for memory file indexing.

All strategies implement the ChunkStrategy protocol:
    def chunk(self, text: str) -> list[str]

The factory function `get_strategy(name, **kwargs)` returns the right instance
based on a name string from config. Adding a new strategy = add a class here
and one entry in the `_REGISTRY` dict at the bottom.

Available strategies:
  "tokens"    — split on approximate token boundaries (whitespace, ~N tokens/chunk)
  "chars"     — split on character count with overlap
  "delimiter" — split on a custom string delimiter (e.g. "---", "\\n\\n")
  "markdown"  — split on markdown block boundaries (headings, fenced code, HR)
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ChunkStrategy(Protocol):
    def chunk(self, text: str) -> list[str]:
        """Split text into a list of non-empty string chunks."""
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nonempty(chunks: list[str]) -> list[str]:
    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Token-approximate chunking
# Splits on whitespace tokens, groups them into chunks of ~chunk_tokens.
# Overlap is expressed in tokens.
# ---------------------------------------------------------------------------

class TokenChunker:
    """
    Approximate token chunking via whitespace splitting.
    1 token ≈ 1 whitespace-delimited word (good enough for embedding purposes).

    Args:
        chunk_tokens: target chunk size in tokens (default 256)
        overlap_tokens: token overlap between consecutive chunks (default 32)
    """

    def __init__(self, chunk_tokens: int = 256, overlap_tokens: int = 32) -> None:
        self.chunk_tokens   = max(1, chunk_tokens)
        self.overlap_tokens = max(0, min(overlap_tokens, chunk_tokens - 1))

    def chunk(self, text: str) -> list[str]:
        words  = text.split()
        if not words:
            return []
        chunks = []
        start  = 0
        while start < len(words):
            end = min(start + self.chunk_tokens, len(words))
            chunks.append(" ".join(words[start:end]))
            if end == len(words):
                break
            start += self.chunk_tokens - self.overlap_tokens
        return _nonempty(chunks)


# ---------------------------------------------------------------------------
# Character chunking
# ---------------------------------------------------------------------------

class CharChunker:
    """
    Fixed-size character chunking with overlap.

    Args:
        chunk_size: characters per chunk (default 800)
        overlap:    character overlap between chunks (default 100)
    """

    def __init__(self, chunk_size: int = 800, overlap: int = 100) -> None:
        self.chunk_size = max(1, chunk_size)
        self.overlap    = max(0, min(overlap, chunk_size - 1))

    def chunk(self, text: str) -> list[str]:
        chunks = []
        start  = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunks.append(text[start:end])
            if end == len(text):
                break
            start += self.chunk_size - self.overlap
        return _nonempty(chunks)


# ---------------------------------------------------------------------------
# Delimiter chunking
# ---------------------------------------------------------------------------

class DelimiterChunker:
    """
    Split on a literal string delimiter. Useful for documents that use a
    consistent separator (e.g. "---", "===", a custom tag).

    Args:
        delimiter: string to split on (default "\\n\\n")
        strip:     strip whitespace from each chunk (default True)
    """

    def __init__(self, delimiter: str = "\n\n", strip: bool = True) -> None:
        self.delimiter = delimiter
        self.strip     = strip

    def chunk(self, text: str) -> list[str]:
        parts = text.split(self.delimiter)
        if self.strip:
            return _nonempty(parts)
        return [p for p in parts if p]


# ---------------------------------------------------------------------------
# Markdown block chunking
# Splits on:
#   - ATX headings (## Heading)
#   - Setext headings (underlined with === or ---)
#   - Horizontal rules (---, ***, ___)
#   - Fenced code block boundaries (``` or ~~~)
# Each split point starts a new chunk. The heading/delimiter line is kept
# at the beginning of its chunk so context is preserved.
# ---------------------------------------------------------------------------

# Matches lines that should start a new chunk
_MD_SPLIT_RE = re.compile(
    r"^(?:"
    r"#{1,6}\s"           # ATX heading
    r"|[=\-]{3,}\s*$"     # Setext underline or HR
    r"|[*_]{3,}\s*$"      # HR with * or _
    r"|```"               # fenced code block
    r"|~~~"               # fenced code block (tilde)
    r")",
    re.MULTILINE,
)


class MarkdownChunker:
    """
    Split markdown text at semantic block boundaries: headings, horizontal
    rules, and fenced code block delimiters.

    Each chunk contains the delimiter line that started it, preserving
    heading context for embedding quality.

    Args:
        min_chunk_chars: chunks shorter than this are merged with the next
                         (avoids tiny chunks for e.g. short section titles).
                         Default 100.
    """

    def __init__(self, min_chunk_chars: int = 100) -> None:
        self.min_chunk_chars = min_chunk_chars

    def chunk(self, text: str) -> list[str]:
        lines  = text.splitlines(keepends=True)
        chunks: list[list[str]] = [[]]

        for line in lines:
            if _MD_SPLIT_RE.match(line) and chunks[-1]:
                chunks.append([])
            chunks[-1].append(line)

        raw = ["".join(c) for c in chunks]

        # Merge chunks that are too short into the next one
        merged: list[str] = []
        buf = ""
        for chunk in raw:
            buf += chunk
            if len(buf) >= self.min_chunk_chars:
                merged.append(buf)
                buf = ""
        if buf:
            if merged:
                merged[-1] += buf
            else:
                merged.append(buf)

        return _nonempty(merged)


# ---------------------------------------------------------------------------
# Registry + factory
# ---------------------------------------------------------------------------

# name -> (class, default kwargs)
_REGISTRY: dict[str, tuple[type, dict]] = {
    "tokens":    (TokenChunker,    {"chunk_tokens": 256,  "overlap_tokens": 32}),
    "chars":     (CharChunker,     {"chunk_size": 800,    "overlap": 100}),
    "delimiter": (DelimiterChunker, {"delimiter": "\n\n", "strip": True}),
    "markdown":  (MarkdownChunker, {"min_chunk_chars": 100}),
}


def get_strategy(name: str, **kwargs) -> ChunkStrategy:
    """
    Return a ChunkStrategy instance by name.
    kwargs override the strategy's default parameters.

    Example:
        get_strategy("tokens", chunk_tokens=512, overlap_tokens=64)
        get_strategy("delimiter", delimiter="---")
        get_strategy("markdown")

    Raises ValueError for unknown strategy names.
    """
    key = name.lower().strip()
    if key not in _REGISTRY:
        valid = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"Unknown chunk strategy '{name}'. Valid options: {valid}"
        )
    cls, defaults = _REGISTRY[key]
    merged = {**defaults, **kwargs}
    return cls(**merged)


def available_strategies() -> list[str]:
    """Return the list of registered strategy names."""
    return sorted(_REGISTRY)

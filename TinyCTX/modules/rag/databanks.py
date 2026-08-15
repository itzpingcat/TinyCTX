"""
modules/rag/databanks.py

DataBank abstraction for the RAG module.

A DataBank is a named, indexable source of text content, backed by a folder
of markdown/text files (*.md, *.txt, *.rst, etc.). Any *.md file may carry a
YAML frontmatter header (see lorefile.py) declaring it a keyword-triggered
lore entry (keys, constant, selective logic, ...); files without frontmatter
behave as plain memory documents, exactly as before.

Legacy SillyTavern lorebook/worldinfo JSON files are auto-converted the first
time they're discovered: one native lore markdown doc is written per active
entry into a same-named folder, and the original JSON is renamed to `.bak`.
From then on it's just a regular folder databank — no JSON support at runtime.

`FilesDataBank` is the only databank kind (the older `LoreBookDataBank` was
retired once legacy JSON started auto-converting to folders on discovery),
so there is no separate protocol/interface layer here — callers just type
against `FilesDataBank` directly.

Public API
----------
  FilesDataBank(name, root, extensions)
  discover_databanks(rag_dir, extensions) -> dict[str, FilesDataBank]
      Scan workspace/rag/ and return all valid databanks by name.
      Converts and unpacks any legacy lorebook JSON found at the root.

Retrieval interface
-------------------
  await bank.rag_search(query, store, embedder, top_k, bm25_weight)
      Full embedding/BM25 search against the folder's chunked index. Used by
      the rag_search tool.

  await bank.auto_inject(text, store, embedder, top_k, bm25_weight)
      For the pre-assemble hook: deterministic ST-style keyword/regex matching
      over any frontmatter entries in the folder, plus (when a store is
      supplied) the same hybrid BM25+vector search rag_search uses — so a
      databank can passively surface relevant chunks even without keyword
      hits. Results from both are merged, deduplicated by file path.

      Each entry's frontmatter `mode` (see lorefile.py) controls which half
      of that applies to it: "keyword"/"regex" entries are deterministic-only
      (never surfaced by the passive embedding search); "vector" entries are
      semantic-only (never fire by keyword); "hybrid" (the default) does both.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from TinyCTX.modules.rag.lorefile import LoreEntry, compile_regex_key, parse_lore_doc

if TYPE_CHECKING:
    from TinyCTX.modules.rag.store import DataStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FilesDataBank — folder of text files
# ---------------------------------------------------------------------------

class FilesDataBank:
    """
    A databank backed by a folder of text files.

    Recursively walks `root` and yields any file whose suffix is in `extensions`.
    Files that cannot be decoded as UTF-8 are skipped with a warning.

    Any *.md file may open with a YAML frontmatter header (see lorefile.py)
    declaring it a keyword-triggered lore entry — `keys`, `constant`,
    `selective`/`selective_logic`, `case_sensitive`, `whole_words`, `disabled`.
    Such entries participate in auto_inject() (ST-style keyword matching) in
    addition to being chunked/indexed like any other file (frontmatter is
    stripped before chunking — only the body is indexed, prefixed with the
    entry's name/keys for recall). Files without frontmatter are indexed as
    plain text and never contribute to auto_inject(), unchanged from before.

    Args:
        name:       Databank identifier (typically the folder name).
        root:       Absolute path to the databank folder.
        extensions: Set of lowercase file extensions to include (e.g. {".md", ".txt"}).
    """

    def __init__(self, name: str, root: Path, extensions: set[str]) -> None:
        self._name       = name
        self._root       = root
        self._extensions = extensions
        # path str -> (mtime at parse time, parsed entry). Avoids re-reading
        # and re-parsing every file on every iter_files()/auto_inject() call
        # (both walk the whole folder, and auto_inject runs once per turn
        # per auto-rag target) when nothing on disk has actually changed.
        self._entry_cache: dict[str, tuple[float, LoreEntry]] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return "files"

    def _iter_entries(self) -> Iterator[tuple[Path, LoreEntry]]:
        seen: set[str] = set()
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self._extensions:
                continue
            key = str(path)
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                logger.warning("[rag/databanks] skipping %s: %s", path, exc)
                continue
            seen.add(key)

            cached = self._entry_cache.get(key)
            if cached is not None and cached[0] == mtime:
                yield path, cached[1]
                continue

            try:
                raw = path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning("[rag/databanks] skipping %s: %s", path, exc)
                self._entry_cache.pop(key, None)
                continue
            entry = parse_lore_doc(raw, path=path) if path.suffix.lower() == ".md" else LoreEntry(content=raw, path=path)
            self._entry_cache[key] = (mtime, entry)
            yield path, entry

        # Drop cache entries for files that no longer exist / no longer match.
        for stale_key in set(self._entry_cache) - seen:
            self._entry_cache.pop(stale_key, None)

    def iter_files(self) -> Iterator[tuple[str, str]]:
        for path, entry in self._iter_entries():
            if entry.disabled:
                continue
            text = "\n".join(filter(None, [
                entry.name,
                ", ".join(entry.keys),
                entry.content,
            ]))
            yield str(path.resolve()), text

    async def rag_search(
        self,
        query: str,
        store: "DataStore",
        embedder,
        top_k: int,
        bm25_weight: float,
        rrf_k: int = 60,
    ) -> list[dict]:
        """Hybrid BM25+vector search against this bank's index."""
        return await _hybrid_search(self._name, query, store, embedder, top_k, bm25_weight, rrf_k)

    async def auto_inject(
        self,
        text: str,
        store: "DataStore | None" = None,
        embedder=None,
        top_k: int = 0,
        bm25_weight: float = 0.3,
        rrf_k: int = 60,
    ) -> list[dict]:
        """
        Deterministic ST-style keyword/regex matching over any frontmatter
        entries in this folder, plus — when `store` is supplied and `top_k` >
        0 — the same hybrid BM25+vector search rag_search uses, so passively
        relevant chunks surface even without a keyword hit. Results are
        merged and deduplicated by file path (keyword/regex hits take
        priority).

        Each entry's `mode` gates which half applies (see lorefile.py):
        "vector" entries skip keyword matching entirely; "keyword"/"regex"
        entries are excluded from the semantic merge below, so they never
        surface passively except by their own deterministic firing.
        """
        results: list[dict] = []
        seen_paths: set[str] = set()
        semantic_excluded: set[str] = set()  # resolved paths of deterministic-only entries

        for path, entry in self._iter_entries():
            if entry.disabled or not entry.path or entry.path.suffix.lower() != ".md":
                continue

            resolved = str(path.resolve())
            if entry.mode in ("keyword", "regex"):
                semantic_excluded.add(resolved)
            if entry.mode == "vector":
                continue  # semantic-only — never fires by keyword/regex

            if not (entry.keys or entry.constant):
                continue  # plain file with no frontmatter — never keyword-triggered
            content = _keyword_match_entry(entry, text)
            if content:
                results.append({"file": self._name, "path": resolved, "text": content, "score": 1.0})
                seen_paths.add(resolved)

        if store is not None and top_k > 0:
            semantic = await _hybrid_search(self._name, text, store, embedder, top_k, bm25_weight, rrf_k)
            for r in semantic:
                if r["path"] in semantic_excluded or r["path"] in seen_paths:
                    continue
                results.append(r)
                seen_paths.add(r["path"])

        return results

    def __repr__(self) -> str:
        return f"FilesDataBank({self._name!r}, root={self._root})"


# ---------------------------------------------------------------------------
# ST-style keyword matching, shared by any frontmatter lore entry
# ---------------------------------------------------------------------------

def _keyword_match_entry(entry: LoreEntry, text: str) -> str | None:
    """
    Return `entry.content` if it fires against `text`, else None.
    Follows SillyTavern selectiveLogic from world-info.js:33-38.

    mode == "regex" matches keys as regular expressions (compile_regex_key);
    every other mode matches them as literal substrings/whole-words, same as
    before mode existed.
    """
    if entry.constant:
        return entry.content

    if entry.mode == "regex":
        any_matches = lambda keys: _any_regex_matches(keys, text)              # noqa: E731
        all_matches = lambda keys: _all_regex_matches(keys, text)              # noqa: E731
    else:
        any_matches = lambda keys: _any_key_matches(                           # noqa: E731
            keys, text, entry.case_sensitive, entry.whole_words)
        all_matches = lambda keys: _all_keys_match(                            # noqa: E731
            keys, text, entry.case_sensitive, entry.whole_words)

    primary_hit = any_matches(entry.keys)

    if not entry.selective:
        fired = primary_hit
    elif entry.selective_logic == 0:  # AND_ANY
        fired = primary_hit or any_matches(entry.secondary_keys)
    elif entry.selective_logic == 1:  # NOT_ALL
        fired = primary_hit and not all_matches(entry.secondary_keys)
    elif entry.selective_logic == 2:  # NOT_ANY
        fired = primary_hit and not any_matches(entry.secondary_keys)
    elif entry.selective_logic == 3:  # AND_ALL
        fired = primary_hit and all_matches(entry.secondary_keys)
    else:
        fired = primary_hit

    return entry.content if fired else None


def _any_key_matches(keys: list[str], text: str, case_sensitive: bool, whole_words: bool) -> bool:
    if not keys:
        return False
    cs, ww = bool(case_sensitive), bool(whole_words)
    haystack = text if cs else text.lower()
    for key in keys:
        needle = key if cs else key.lower()
        if ww:
            flags = 0 if cs else re.IGNORECASE
            if re.search(r"\b" + re.escape(needle) + r"\b", haystack if cs else text, flags):
                return True
        elif needle in haystack:
            return True
    return False


def _all_keys_match(keys: list[str], text: str, case_sensitive: bool, whole_words: bool) -> bool:
    """Return True only if every key in `keys` matches `text`."""
    if not keys:
        return False
    cs, ww = bool(case_sensitive), bool(whole_words)
    haystack = text if cs else text.lower()
    for key in keys:
        needle = key if cs else key.lower()
        if ww:
            flags = 0 if cs else re.IGNORECASE
            if not re.search(r"\b" + re.escape(needle) + r"\b", haystack if cs else text, flags):
                return False
        elif needle not in haystack:
            return False
    return True


def _any_regex_matches(keys: list[str], text: str) -> bool:
    """mode == 'regex' equivalent of _any_key_matches: keys are patterns, not literals."""
    for key in keys:
        pattern = compile_regex_key(key)
        if pattern is not None and pattern.search(text):
            return True
    return False


def _all_regex_matches(keys: list[str], text: str) -> bool:
    """mode == 'regex' equivalent of _all_keys_match."""
    if not keys:
        return False
    for key in keys:
        pattern = compile_regex_key(key)
        if pattern is None or not pattern.search(text):
            return False
    return True


# ---------------------------------------------------------------------------
# Shared retrieval helper
# ---------------------------------------------------------------------------

async def _hybrid_search(
    bank_name: str,
    query: str,
    store: "DataStore",
    embedder,
    top_k: int,
    bm25_weight: float,
    rrf_k: int = 60,
) -> list[dict]:
    """Run hybrid BM25+vector search against `store`. Used by both bank types."""
    q_vec = None
    if embedder is not None:
        try:
            q_vec = (await embedder.embed([query], priority=5, kind="query"))[0]
        except Exception as exc:
            logger.warning("[rag/databanks] embed failed for '%s': %s — BM25 only", bank_name, exc)
    try:
        return store.hybrid_search(query, q_vec, top_k, bm25_weight, rrf_k=rrf_k)
    except Exception as exc:
        logger.warning("[rag/databanks] search failed for '%s': %s", bank_name, exc)
        return []


# ---------------------------------------------------------------------------
# Legacy lorebook detection + one-time conversion
# ---------------------------------------------------------------------------

def _is_lorebook_json(path: Path) -> bool:
    """
    Return True if `path` looks like a SillyTavern lorebook JSON.
    Minimal check: valid JSON with a top-level 'entries' key that is a dict or list.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(raw.get("entries"), (dict, list))


def _convert_and_backup(json_path: Path, dest_dir: Path) -> bool:
    """
    Convert a legacy lorebook JSON into native lore docs under `dest_dir`,
    then rename the JSON to `.bak` so it's never re-converted. Returns True
    on success (dest_dir now holds the converted docs).
    """
    from TinyCTX.modules.rag.lorefile import convert_lorebook_json

    written = convert_lorebook_json(json_path, dest_dir)
    if written == 0:
        logger.warning(
            "[rag/databanks] conversion of %s produced no entries — leaving JSON in place",
            json_path,
        )
        return False

    bak_path = json_path.with_suffix(json_path.suffix + ".bak")
    try:
        json_path.rename(bak_path)
    except Exception as exc:
        logger.error(
            "[rag/databanks] converted %s but failed to rename to %s: %s",
            json_path, bak_path, exc,
        )
        return False

    logger.info(
        "[rag/databanks] converted lorebook %s -> %s/ (%d entries), original backed up to %s",
        json_path.name, dest_dir.name, written, bak_path.name,
    )
    return True


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_databanks(rag_dir: Path, extensions: set[str]) -> dict[str, "FilesDataBank"]:
    """
    Scan `rag_dir` and return a dict of {name: FilesDataBank} for all valid sources.

    Rules:
      - A subdirectory of rag_dir                -> FilesDataBank named after the folder.
      - A *.json that passes lorebook validation -> converted in place into a
        same-named native-format folder, original renamed to `.json.bak`, then
        treated as that FilesDataBank.
      - A *.json that fails validation           -> warning logged, skipped.
      - Other files at root level                -> debug logged, ignored.
      - rag_dir doesn't exist yet                -> returns empty dict (no error).

    The .cache directory is always excluded.
    """
    if not rag_dir.exists():
        return {}

    result: dict[str, FilesDataBank] = {}

    for entry in sorted(rag_dir.iterdir()):
        if entry.name.startswith(".") or entry.name == ".cache":
            continue

        if entry.is_file() and entry.suffix.lower() == ".json":
            if not _is_lorebook_json(entry):
                logger.warning(
                    "[rag/databanks] skipping %s — not a recognised lorebook JSON "
                    "(expected top-level 'entries' dict or list)",
                    entry.name,
                )
                continue
            dest_dir = rag_dir / entry.stem
            if not _convert_and_backup(entry, dest_dir):
                continue
            bank: FilesDataBank = FilesDataBank(name=entry.stem, root=dest_dir, extensions=extensions)
            result[entry.stem] = bank
            logger.debug("[rag/databanks] discovered converted lorebook folder: %s", entry.stem)

        elif entry.is_dir():
            bank = FilesDataBank(name=entry.name, root=entry, extensions=extensions)
            result[entry.name] = bank
            logger.debug("[rag/databanks] discovered FilesDataBank: %s", entry.name)

        elif entry.is_file():
            logger.debug("[rag/databanks] ignoring %s — not a directory or .json", entry.name)

    return result

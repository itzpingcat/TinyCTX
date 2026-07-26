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

Public API
----------
  DataBank (Protocol)        — duck-typed interface
  FilesDataBank(name, root, extensions)
  discover_databanks(rag_dir, extensions) -> dict[str, DataBank]
      Scan workspace/rag/ and return all valid databanks by name.
      Converts and unpacks any legacy lorebook JSON found at the root.

Retrieval interface
-------------------
  await bank.rag_search(query, store, embedder, top_k, bm25_weight)
      Full embedding/BM25 search against the folder's chunked index. Used by
      the rag_search tool.

  bank.auto_inject(text)
      Fast, synchronous ST-style keyword matching over any frontmatter
      entries in the folder, for the pre-assemble hook. Folders with no
      frontmatter entries simply return [].
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Protocol, runtime_checkable

from TinyCTX.modules.rag.lorefile import LoreEntry, parse_lore_doc

if TYPE_CHECKING:
    from TinyCTX.modules.rag.store import DataStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DataBank(Protocol):
    """
    A named, indexable source of text content.

    name         — identifier used in tool calls (e.g. "lore", "characters")
    kind         — "files" | "lorebook" | etc.
    iter_files() — yields (path_str, content) pairs for all indexable items
    """

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    def iter_files(self) -> Iterator[tuple[str, str]]:
        """
        Yield (path_str, text_content) for each indexable item.
        path_str is a stable, unique string key used by the store (e.g. absolute path).
        text_content is the full text to chunk and index.
        """
        ...

    async def rag_search(
        self,
        query: str,
        store: "DataStore",
        embedder,
        top_k: int,
        bm25_weight: float,
    ) -> list[dict]:
        """Run a hybrid BM25+vector search and return result dicts."""
        ...

    def auto_inject(self, text: str) -> list[dict]:
        """
        Synchronous retrieval for the pre-assemble hook.
        Returns result dicts {file, path, text, score}, or [] if not supported.
        """
        ...


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

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return "files"

    def _iter_entries(self) -> Iterator[tuple[Path, LoreEntry]]:
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self._extensions:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning("[rag/databanks] skipping %s: %s", path, exc)
                continue
            entry = parse_lore_doc(raw, path=path) if path.suffix.lower() == ".md" else LoreEntry(content=raw, path=path)
            yield path, entry

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
    ) -> list[dict]:
        """Hybrid BM25+vector search against this bank's index."""
        return await _hybrid_search(self._name, query, store, embedder, top_k, bm25_weight)

    def auto_inject(self, text: str) -> list[dict]:
        """ST-style keyword matching over any frontmatter entries in this folder."""
        results = []
        for path, entry in self._iter_entries():
            if entry.disabled or not entry.path or entry.path.suffix.lower() != ".md":
                continue
            if not (entry.keys or entry.constant):
                continue  # plain file with no frontmatter — never auto-injected
            content = _keyword_match_entry(entry, text)
            if content:
                results.append({"file": self._name, "path": str(path), "text": content, "score": 1.0})
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
    """
    if entry.constant:
        return entry.content

    primary_hit = _any_key_matches(entry.keys, text, entry.case_sensitive, entry.whole_words)

    if not entry.selective:
        fired = primary_hit
    elif entry.selective_logic == 0:  # AND_ANY
        secondary_hit = _any_key_matches(entry.secondary_keys, text, entry.case_sensitive, entry.whole_words)
        fired = primary_hit or secondary_hit
    elif entry.selective_logic == 1:  # NOT_ALL
        all_secondary = _all_keys_match(entry.secondary_keys, text, entry.case_sensitive, entry.whole_words)
        fired = primary_hit and not all_secondary
    elif entry.selective_logic == 2:  # NOT_ANY
        secondary_hit = _any_key_matches(entry.secondary_keys, text, entry.case_sensitive, entry.whole_words)
        fired = primary_hit and not secondary_hit
    elif entry.selective_logic == 3:  # AND_ALL
        all_secondary = _all_keys_match(entry.secondary_keys, text, entry.case_sensitive, entry.whole_words)
        fired = primary_hit and all_secondary
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
) -> list[dict]:
    """Run hybrid BM25+vector search against `store`. Used by both bank types."""
    q_vec = None
    if embedder is not None:
        try:
            q_vec = (await embedder.embed([query], priority=5, kind="query"))[0]
        except Exception as exc:
            logger.warning("[rag/databanks] embed failed for '%s': %s — BM25 only", bank_name, exc)
    try:
        return store.hybrid_search(query, q_vec, top_k, bm25_weight)
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

def discover_databanks(rag_dir: Path, extensions: set[str]) -> dict[str, "DataBank"]:
    """
    Scan `rag_dir` and return a dict of {name: DataBank} for all valid sources.

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

    result: dict[str, DataBank] = {}

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
            bank: DataBank = FilesDataBank(name=entry.stem, root=dest_dir, extensions=extensions)
            result[entry.stem] = bank
            logger.debug("[rag/databanks] discovered converted lorebook folder: %s", entry.stem)

        elif entry.is_dir():
            bank = FilesDataBank(name=entry.name, root=entry, extensions=extensions)
            result[entry.name] = bank
            logger.debug("[rag/databanks] discovered FilesDataBank: %s", entry.name)

        elif entry.is_file():
            logger.debug("[rag/databanks] ignoring %s — not a directory or .json", entry.name)

    return result

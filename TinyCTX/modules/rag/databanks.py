"""
modules/rag/databanks.py

DataBank abstraction for the RAG module.

A DataBank is a named, indexable source of text content. Two implementations
are provided here:

  FilesDataBank     — a folder of text files (*.md, *.txt, *.rst, etc.)
  WorldInfoDataBank — a SillyTavern worldinfo JSON file (stub; implemented later)

Public API
----------
  DataBank (Protocol)        — duck-typed interface
  FilesDataBank(name, root, extensions)
  WorldInfoDataBank(name, path)   — raises NotImplementedError until implemented
  discover_databanks(rag_dir, extensions) -> dict[str, DataBank]
      Scan workspace/rag/ and return all valid databanks by name.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DataBank(Protocol):
    """
    A named, indexable source of text content.

    name         — identifier used in tool calls (e.g. "lore", "characters")
    kind         — "files" | "worldinfo" | etc.
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


# ---------------------------------------------------------------------------
# FilesDataBank — folder of text files
# ---------------------------------------------------------------------------

class FilesDataBank:
    """
    A databank backed by a folder of text files.

    Recursively walks `root` and yields any file whose suffix is in `extensions`.
    Files that cannot be decoded as UTF-8 are skipped with a warning.

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

    def iter_files(self) -> Iterator[tuple[str, str]]:
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self._extensions:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning("[rag/databanks] skipping %s: %s", path, exc)
                continue
            yield str(path.resolve()), content

    def __repr__(self) -> str:
        return f"FilesDataBank({self._name!r}, root={self._root})"


# ---------------------------------------------------------------------------
# WorldInfoDataBank — SillyTavern worldinfo JSON (stub)
# ---------------------------------------------------------------------------

class WorldInfoDataBank:
    """
    A databank backed by a SillyTavern worldinfo JSON file.

    NOT YET IMPLEMENTED — will be wired in a follow-up session once the
    worldinfo format documentation has been reviewed.

    Attempting to iterate files raises NotImplementedError.
    The databank is discovered and registered normally; it just can't be indexed yet.
    """

    def __init__(self, name: str, path: Path) -> None:
        self._name = name
        self._path = path

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return "worldinfo"

    def iter_files(self) -> Iterator[tuple[str, str]]:
        raise NotImplementedError(
            f"WorldInfoDataBank '{self._name}' is not yet implemented. "
            "It will be added after SillyTavern worldinfo format review."
        )

    def __repr__(self) -> str:
        return f"WorldInfoDataBank({self._name!r}, path={self._path})"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_databanks(rag_dir: Path, extensions: set[str]) -> dict[str, "DataBank"]:
    """
    Scan `rag_dir` and return a dict of {name: DataBank} for all valid sources.

    Rules:
      - A subdirectory of rag_dir   -> FilesDataBank named after the folder.
      - A *.json file in rag_dir    -> WorldInfoDataBank named after the stem.
      - Other files at root level   -> ignored.
      - rag_dir doesn't exist yet   -> returns empty dict (no error).

    The .cache directory is always excluded.
    """
    if not rag_dir.exists():
        return {}

    result: dict[str, DataBank] = {}

    for entry in sorted(rag_dir.iterdir()):
        if entry.name.startswith(".") or entry.name == ".cache":
            continue

        if entry.is_dir():
            bank: DataBank = FilesDataBank(name=entry.name, root=entry, extensions=extensions)
            result[entry.name] = bank
            logger.debug("[rag/databanks] discovered FilesDataBank: %s", entry.name)

        elif entry.is_file() and entry.suffix.lower() == ".json":
            bank = WorldInfoDataBank(name=entry.stem, path=entry)
            result[entry.stem] = bank
            logger.debug(
                "[rag/databanks] discovered WorldInfoDataBank (stub): %s", entry.stem
            )

    return result

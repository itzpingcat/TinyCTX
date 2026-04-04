"""
modules/memory/indexer.py

Async indexer — walks workspace/memory/**/*.md, detects dirty files via
MemoryStore.is_dirty(), re-chunks and re-embeds them, then commits to the store.

Design notes
------------
- Fully async: embedding calls go through ai.Embedder (aiohttp).
- Lazy: sync() is a no-op until called. The pre_assemble hook in __main__.py
  calls sync() before every retrieval, so the first turn triggers indexing.
- Embedder is optional: if None (no embedding model configured), chunks are
  stored without vectors and only BM25 search is available.
- Concurrency: files are embedded sequentially to avoid hammering the
  embedding server. Per-file parallelism can be added later if needed.
- The indexer never modifies .md files — read-only access to the memory dir.

Public API
----------
    indexer = MemoryIndexer(
        store          = store,           # MemoryStore instance
        memory_dir     = Path("~/.tinyctx/memory"),
        strategy       = get_strategy("markdown"),
        embedder       = embedder_or_none,
        embedding_model = "nomic-embed-text",  # model name string for dirty check
    )
    await indexer.sync()   # call before every retrieval
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from modules.memory.store import MemoryStore
from modules.memory.chunkers import ChunkStrategy

logger = logging.getLogger(__name__)


class MemoryIndexer:
    """
    Walks workspace/memory/**/*.md, detects dirty files, and (re-)indexes them.

    Args:
        store:           MemoryStore instance to read/write.
        memory_dir:      Root directory to scan recursively for *.md files.
        strategy:        ChunkStrategy instance (from chunkers.get_strategy).
        embedder:        ai.Embedder instance, or None for BM25-only mode.
        embedding_model: Model name string — stored per-file for dirty detection.
                         Should match the `model` field of the ModelConfig used
                         to build the embedder. Pass "" when embedder is None.
    """

    def __init__(
        self,
        store:           MemoryStore,
        memory_dir:      Path,
        strategy:        ChunkStrategy,
        embedder,                          # ai.Embedder | None
        embedding_model: str = "",
    ) -> None:
        self._store           = store
        self._memory_dir      = memory_dir
        self._strategy        = strategy
        self._embedder        = embedder
        self._embedding_model = embedding_model
        self._sync_lock       = asyncio.Lock()

    async def sync(self) -> None:
        """
        Async-safe sync. Multiple concurrent callers are serialised by a lock
        so only one full scan runs at a time.
        """
        async with self._sync_lock:
            await self._sync_inner()

    async def _sync_inner(self) -> None:
        if not self._memory_dir.exists():
            logger.debug("[memory/indexer] memory_dir does not exist yet: %s", self._memory_dir)
            return

        # Collect all .md files recursively
        disk_paths: set[str] = {
            str(p.resolve())
            for p in self._memory_dir.rglob("*.md")
            if p.is_file()
        }

        # Remove rows for files that were deleted from disk
        removed = self._store.remove_deleted_files(disk_paths)
        if removed:
            logger.info("[memory/indexer] removed %d deleted file(s) from index", len(removed))
            self._store.commit()

        # Index dirty files
        dirty: list[tuple[Path, str, str]] = []
        for path_str in sorted(disk_paths):
            path = Path(path_str)
            try:
                content      = path.read_text(encoding="utf-8")
                content_hash = _md5(content)
                if self._store.is_dirty(path_str, content_hash, self._embedding_model):
                    dirty.append((path, content, content_hash))
            except Exception as exc:
                logger.warning("[memory/indexer] could not read %s: %s", path, exc)

        if not dirty:
            logger.debug("[memory/indexer] all files up to date (%d total)", len(disk_paths))
            return

        logger.info("[memory/indexer] indexing %d dirty file(s)", len(dirty))
        for path, content, content_hash in dirty:
            await self._index_file(path, content, content_hash)

    async def _index_file(self, path: Path, content: str, content_hash: str) -> None:
        path_str = str(path.resolve())
        mtime        = path.stat().st_mtime
        chunks       = self._strategy.chunk(content)

        if not chunks:
            logger.debug("[memory/indexer] no chunks produced for %s — skipping", path.name)
            return

        # Embed chunks if an embedder is configured
        embeddings: list[list[float]] | None = None
        if self._embedder is not None:
            try:
                embeddings = await self._embedder.embed(chunks)
            except Exception as exc:
                logger.warning(
                    "[memory/indexer] embedding failed for %s: %s — storing without vectors",
                    path.name, exc,
                )

        # Atomically replace old data for this file
        self._store.delete_file(path_str)
        self._store.upsert_file(path_str, content_hash, self._embedding_model, mtime)
        self._store.insert_chunks(path_str, chunks, embeddings)
        self._store.commit()

        vec_status = f"{len(embeddings)} vectors" if embeddings is not None else "no vectors (BM25 only)"
        logger.info(
            "[memory/indexer] indexed %s — %d chunk(s), %s",
            path.name, len(chunks), vec_status,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

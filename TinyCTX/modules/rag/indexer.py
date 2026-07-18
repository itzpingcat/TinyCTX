"""
modules/rag/indexer.py

Async indexer — walks a DataBank's files, detects dirty entries via
DataStore.is_dirty(), re-chunks and re-embeds them, then commits to the store.

One DataBankIndexer instance per databank. The RAG __main__ creates and caches
one indexer per (name, store) pair.

Design notes
------------
- Fully async: embedding calls go through ai.Embedder (aiohttp).
- Lazy: sync() is a no-op if nothing is dirty.
- Embedder is optional: if None, chunks are stored without vectors and only
  BM25 search is available.
- Files that cannot be read are skipped with a warning (already handled by
  DataBank.iter_files()).
- Any DataBank stub that raises NotImplementedError from iter_files() is caught
  and logged; startup is not broken.

Public API
----------
    indexer = DataBankIndexer(
        store           = store,           # DataStore instance
        databank        = databank,        # DataBank instance
        strategy        = strategy,        # ChunkStrategy instance
        embedder        = embedder_or_none,
        embedding_model = "nomic-embed-text",
    )
    await indexer.sync()   # call before every retrieval
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from TinyCTX.modules.rag.store import DataStore
from TinyCTX.modules.rag.chunkers import ChunkStrategy
from TinyCTX.modules.rag.databanks import DataBank

logger = logging.getLogger(__name__)


class DataBankIndexer:
    """
    Indexes a single DataBank into a DataStore.

    Args:
        store:           DataStore instance to read/write.
        databank:        DataBank to index.
        strategy:        ChunkStrategy instance (from chunkers.get_strategy).
        embedder:        ai.Embedder instance, or None for BM25-only mode.
        embedding_model: Model name string stored per-file for dirty detection.
                         Pass "" when embedder is None.
    """

    def __init__(
        self,
        store:           DataStore,
        databank:        DataBank,
        strategy:        ChunkStrategy,
        embedder,                          # ai.Embedder | None
        embedding_model: str = "",
    ) -> None:
        self._store           = store
        self._databank        = databank
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
        # Collect current files from the databank
        try:
            file_items = list(self._databank.iter_files())
        except NotImplementedError:
            logger.debug(
                "[rag/indexer] skipping databank '%s' (not yet implemented)",
                self._databank.name,
            )
            return
        except Exception as exc:
            logger.warning(
                "[rag/indexer] error iterating databank '%s': %s",
                self._databank.name, exc,
            )
            return

        disk_paths: set[str] = {path_str for path_str, _ in file_items}

        # Remove rows for files that were deleted from disk
        removed = self._store.remove_deleted_files(disk_paths)
        if removed:
            logger.info(
                "[rag/indexer] [%s] removed %d deleted file(s)",
                self._databank.name, len(removed),
            )
            self._store.commit()

        # Index dirty files
        dirty: list[tuple[str, str, str]] = []
        for path_str, content in file_items:
            content_hash = _md5(content)
            if self._store.is_dirty(path_str, content_hash, self._embedding_model):
                dirty.append((path_str, content, content_hash))

        if not dirty:
            logger.debug(
                "[rag/indexer] [%s] all files up to date (%d total)",
                self._databank.name, len(disk_paths),
            )
            return

        logger.info(
            "[rag/indexer] [%s] indexing %d dirty file(s)",
            self._databank.name, len(dirty),
        )
        embedder_down = False
        indexed = 0
        for path_str, content, content_hash in dirty:
            if embedder_down:
                # Already know the embedder is unreachable this sync pass —
                # don't hammer it once per remaining file, and don't mark
                # them indexed (they must stay dirty so a later sync with a
                # healthy embedder actually retries them).
                continue
            try:
                ok = await self._index_file(path_str, content, content_hash)
            except Exception:
                # Any embedder call failing here means it's almost certainly
                # unreachable for the rest of this batch too — stop trying
                # instead of repeating the same failure per file.
                embedder_down = True
                logger.warning(
                    "[rag/indexer] [%s] embedder unreachable — skipping remaining "
                    "dirty file(s) this sync, will retry next sync",
                    self._databank.name,
                )
                continue
            if ok:
                indexed += 1
        if embedder_down and indexed == 0:
            logger.warning(
                "[rag/indexer] [%s] no files indexed — embedder unreachable",
                self._databank.name,
            )

    async def _index_file(self, path_str: str, content: str, content_hash: str) -> bool:
        """
        Index one file. Returns True if it was actually indexed (with or
        without vectors from an absent embedder), False if nothing was
        written. Raises if the embedder call fails so the caller can stop
        retrying the rest of the batch — callers must NOT catch this and
        still call upsert_file, or a failed embed gets silently recorded as
        done and the file is never retried.
        """
        path   = Path(path_str)
        mtime  = path.stat().st_mtime if path.exists() else 0.0
        chunks = self._strategy.chunk(content)

        if not chunks:
            logger.debug(
                "[rag/indexer] [%s] no chunks from %s — skipping",
                self._databank.name, path_str,
            )
            return False

        embeddings: list[list[float]] | None = None
        if self._embedder is not None:
            # Let embed() failures propagate — caller decides whether to
            # keep going. We must not upsert_file() with a "success" hash
            # when the embedding never happened.
            embeddings = await self._embedder.embed(chunks, priority=20)

        self._store.delete_file(path_str)
        self._store.upsert_file(path_str, content_hash, self._embedding_model, mtime)
        self._store.insert_chunks(path_str, chunks, embeddings)
        self._store.commit()

        vec_status = f"{len(embeddings)} vectors" if embeddings is not None else "no vectors (no embedder configured)"
        logger.info(
            "[rag/indexer] [%s] indexed %s — %d chunk(s), %s",
            self._databank.name, Path(path_str).name, len(chunks), vec_status,
        )
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

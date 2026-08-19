"""
tool_handling/vector_store.py

SQLite-backed embedding cache for the tool registry — one row per tool name,
not per chunk, since a tool definition (name + description + parameter
schema) is already atomic; there's nothing to split the way rag/store.py
splits a file into chunks.

Same on-disk shapes as modules/rag/store.py (BM25 via FTS5, embeddings as
little-endian float32 BLOBs, numpy-accelerated cosine with a pure-Python
fallback) deliberately reused rather than reinvented — this file is a
smaller, single-table version of that store.

Location: <config.data.path>/tools_vector_cache.db — sibling to agent.db,
not under workspace/, because the tool registry is derived from installed
modules (process-internal state), not user-authored content the way
workspace/rag/'s databanks are.

Lifecycle: one ToolVectorStore is built once per process by Runtime (see
runtime.py) and handed to every AgentCycle's ToolCallHandler. The tool
corpus itself (name/description/schema) is rebuilt fresh every turn by
module_registry.register_agent(), but embeddings are content-hash cached
here across turns and across process restarts — re-embedding only happens
when a tool's registered text actually changes.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
import time
from pathlib import Path

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import numpy as np
try:
    import numpy as np  # type: ignore[no-redef]
    _NUMPY = True
except ImportError:
    _NUMPY = False


_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS tools (
    name            TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    embedding_model TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL,   -- the exact string that was embedded/hashed
    embedding       BLOB,            -- little-endian float32 array, NULL if no embedder
    indexed_at      REAL NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
    text,
    content       = 'tools',
    content_rowid = 'rowid'
);

CREATE TRIGGER IF NOT EXISTS tools_ai AFTER INSERT ON tools BEGIN
    INSERT INTO tools_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS tools_ad AFTER DELETE ON tools BEGIN
    INSERT INTO tools_fts(tools_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS tools_au AFTER UPDATE OF text ON tools BEGIN
    INSERT INTO tools_fts(tools_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO tools_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""


def content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _vec_to_blob(vec: list[float]) -> bytes:
    if _NUMPY:
        return np.array(vec, dtype=np.float32).tobytes()
    return struct.pack(f"<{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    if _NUMPY:
        return np.frombuffer(blob, dtype=np.float32).tolist()
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


class ToolVectorStore:
    """
    Thread-safe (check_same_thread=False) SQLite store mapping tool name ->
    (embedded text, embedding vector). One instance per process, owned by
    Runtime and shared read/write across every AgentCycle's ToolCallHandler.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def is_dirty(self, name: str, text_hash: str, embedding_model: str) -> bool:
        """True if `name` needs (re)embedding: never indexed, its embedded
        text changed, or the embedding model changed."""
        row = self._conn.execute(
            "SELECT content_hash, embedding_model FROM tools WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return True
        stored_hash, stored_model = row
        return stored_hash != text_hash or stored_model != embedding_model

    def known_names(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT name FROM tools").fetchall()}

    def upsert(
        self,
        name: str,
        text: str,
        text_hash: str,
        embedding_model: str,
        embedding: list[float] | None,
    ) -> None:
        blob = _vec_to_blob(embedding) if embedding is not None else None
        self._conn.execute(
            """
            INSERT INTO tools(name, content_hash, embedding_model, text, embedding, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                content_hash    = excluded.content_hash,
                embedding_model = excluded.embedding_model,
                text            = excluded.text,
                embedding       = excluded.embedding,
                indexed_at      = excluded.indexed_at
            """,
            (name, text_hash, embedding_model, text, blob, time.time()),
        )

    def remove_stale(self, current_names: set[str]) -> list[str]:
        """Delete rows for tools no longer registered (module unloaded, etc.).
        Returns the names removed."""
        stale = [n for n in self.known_names() if n not in current_names]
        for n in stale:
            self._conn.execute("DELETE FROM tools WHERE name = ?", (n,))
        return stale

    def commit(self) -> None:
        self._conn.commit()

    # ------------------------------------------------------------------
    # Search primitives — kept separate from fusion logic (see search.py)
    # ------------------------------------------------------------------

    def bm25_search(self, query: str, limit: int) -> list[tuple[str, float]]:
        """FTS5 BM25 search. Returns [(name, score)], higher = better."""
        fts_query = self._to_fts_query(query)
        if not fts_query:
            return []
        rows = self._conn.execute(
            """
            SELECT t.name, -bm25(tools_fts)
            FROM   tools_fts
            JOIN   tools t ON tools_fts.rowid = t.rowid
            WHERE  tools_fts MATCH ?
            ORDER  BY bm25(tools_fts)
            LIMIT  ?
            """,
            (fts_query, limit),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def vector_search(self, query_vec: list[float], limit: int) -> list[tuple[str, float]]:
        """Cosine similarity over every embedded row. Returns [(name, score)]
        descending. Empty if no rows have an embedding."""
        rows = self._conn.execute(
            "SELECT name, embedding FROM tools WHERE embedding IS NOT NULL"
        ).fetchall()
        if not rows:
            return []
        scored = _cosine_all(query_vec, rows)
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ToolVectorStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def _to_fts_query(query: str) -> str:
        tokens = [t for t in query.split() if t]
        if not tokens:
            return ""
        escaped = [t.replace('"', '""') for t in tokens]
        return " OR ".join(f'"{token}"' for token in escaped)


def _cosine_all(query: list[float], rows: list[tuple[str, bytes]]) -> list[tuple[str, float]]:
    """Cosine similarity between `query` and every (name, blob) row. Corpus
    here is the tool registry — dozens of entries at most — so this is a
    single small pass; no need for rag/store.py's matrix-multiply path at
    this scale, but numpy is used when available anyway for consistency."""
    if not rows:
        return []

    if _NUMPY:
        names = [r[0] for r in rows]
        matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        q = np.array(query, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return [(n, 0.0) for n in names]
        q = q / q_norm
        row_norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        row_norms = np.where(row_norms == 0, 1.0, row_norms)
        matrix = matrix / row_norms
        scores = matrix @ q
        return list(zip(names, scores.tolist()))

    q_mag = math.sqrt(sum(x * x for x in query))
    if q_mag == 0:
        return [(r[0], 0.0) for r in rows]
    out = []
    for name, blob in rows:
        vec = _blob_to_vec(blob)
        dot = sum(a * b for a, b in zip(query, vec))
        v_mag = math.sqrt(sum(x * x for x in vec))
        out.append((name, (dot / (q_mag * v_mag)) if v_mag else 0.0))
    return out

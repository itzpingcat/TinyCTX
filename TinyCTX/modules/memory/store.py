"""
modules/memory/store.py

SQLite-backed chunk store for the memory module.

Responsibilities:
  - Schema creation and migration
  - File-level dirty detection (content hash + embedding model name)
  - Chunk insert / delete / query
  - BM25 full-text search via FTS5
  - Vector cosine similarity search
  - Hybrid scoring (BM25 + vector)

Embedding storage
-----------------
Embeddings are stored as raw float32 binary blobs (BLOB column), not JSON.
This is ~4x smaller on disk and eliminates parse overhead on every search.

Serialisation uses numpy if available (fast), otherwise falls back to the
stdlib `struct` module (zero extra dependencies). Both paths produce
identical byte layouts (little-endian float32 array), so the DB is portable
regardless of which path wrote it.

Cosine similarity
-----------------
When numpy is available, all chunk vectors are loaded into a 2-D matrix and
the query cosine is computed in one vectorised operation — O(N) numpy work
instead of O(N) Python loops. Falls back to a pure-Python dot-product loop
when numpy is absent.

Schema
------
files:
    path            TEXT PK  — absolute resolved path
    content_hash    TEXT     — MD5 hex of file contents
    embedding_model TEXT     — model string used when embedding (for dirty check)
    mtime           REAL     — st_mtime at index time (informational)
    indexed_at      REAL     — unix timestamp of last successful index

chunks:
    id              INTEGER PK AUTOINCREMENT
    file_path       TEXT     — FK → files.path  (CASCADE DELETE)
    chunk_index     INTEGER  — position within file (0-based)
    text            TEXT     — raw chunk text
    embedding       BLOB     — little-endian float32 array, NULL if no embedder

chunks_fts: FTS5 virtual table over chunks.text for BM25.

Migration
---------
If an existing DB has embedding as TEXT (JSON), _migrate() converts it to
BLOB on first open so old DBs are upgraded automatically.
"""
from __future__ import annotations

import math
import sqlite3
import struct
import time
from pathlib import Path

_LN2 = math.log(2)

# numpy is optional — used for fast vectorised cosine when present
try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    embedding_model TEXT NOT NULL DEFAULT '',
    mtime           REAL NOT NULL DEFAULT 0,
    indexed_at      REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT    NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    embedding   BLOB             -- little-endian float32 array, NULL when no embedder
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content       = 'chunks',
    content_rowid = 'id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


# ---------------------------------------------------------------------------
# Float32 blob serialisation (stdlib fallback)
# ---------------------------------------------------------------------------

def _vec_to_blob(vec: list[float]) -> bytes:
    """Encode a float list as a little-endian float32 byte array."""
    if _NUMPY:
        return np.array(vec, dtype=np.float32).tobytes()
    return struct.pack(f"<{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    """Decode a little-endian float32 byte array back to a Python float list."""
    if _NUMPY:
        return np.frombuffer(blob, dtype=np.float32).tolist()
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

def _cosine_matrix(query: list[float], rows: list[tuple]) -> dict[int, float]:
    """
    Compute cosine similarity between query and all stored vectors.

    rows: list of (chunk_id, file_path, text, blob)

    Returns {chunk_id: score}.

    Uses a single numpy matrix multiply when numpy is available, giving
    O(N) work in fast C code rather than O(N) Python loops.
    """
    if not rows:
        return {}

    ids = [r[0] for r in rows]

    if _NUMPY:
        # Stack all blobs into a (N, D) float32 matrix — no Python loop over dims
        matrix = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])  # (N, D)
        q      = np.array(query, dtype=np.float32)                                # (D,)

        # Normalise
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return {cid: 0.0 for cid in ids}
        q = q / q_norm

        row_norms = np.linalg.norm(matrix, axis=1, keepdims=True)          # (N, 1)
        row_norms = np.where(row_norms == 0, 1.0, row_norms)               # avoid /0
        matrix    = matrix / row_norms                                      # (N, D) normalised

        scores = matrix @ q                                                 # (N,) dot products
        return dict(zip(ids, scores.tolist()))

    # Pure-Python fallback
    q     = query
    q_mag = math.sqrt(sum(x * x for x in q))
    if q_mag == 0:
        return {cid: 0.0 for cid in ids}

    result: dict[int, float] = {}
    for cid, _, _, blob in rows:
        vec   = _blob_to_vec(blob)
        dot   = sum(a * b for a, b in zip(q, vec))
        v_mag = math.sqrt(sum(x * x for x in vec))
        result[cid] = (dot / (q_mag * v_mag)) if v_mag else 0.0
    return result


# ---------------------------------------------------------------------------
# Migration: JSON TEXT → float32 BLOB
# ---------------------------------------------------------------------------

def _migrate(conn: sqlite3.Connection) -> None:
    """
    If the chunks table has a TEXT embedding column (old JSON format), convert
    every non-NULL value to a float32 BLOB in place.

    Safe to run on an already-migrated DB — it checks column type first.
    """
    import json as _json

    col_info = conn.execute("PRAGMA table_info(chunks)").fetchall()
    # col_info rows: (cid, name, type, notnull, dflt_value, pk)
    emb_col = next((c for c in col_info if c[1] == "embedding"), None)
    if emb_col is None or emb_col[2].upper() == "BLOB":
        return  # already correct schema or column missing

    # TEXT column found — convert row by row
    rows = conn.execute(
        "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL"
    ).fetchall()

    if not rows:
        return

    converted = 0
    for row_id, raw in rows:
        if isinstance(raw, (bytes, memoryview)):
            continue  # already binary somehow
        try:
            vec  = _json.loads(raw)
            blob = _vec_to_blob(vec)
            conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (blob, row_id))
            converted += 1
        except Exception:
            pass  # leave corrupt rows as-is

    if converted:
        conn.commit()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class MemoryStore:
    """
    Thread-safe (check_same_thread=False) SQLite store for memory chunks.
    One instance is shared between MemoryIndexer and the retrieval code.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        _migrate(self._conn)

    # ------------------------------------------------------------------
    # Dirty detection
    # ------------------------------------------------------------------

    def is_dirty(self, path: str, content_hash: str, embedding_model: str) -> bool:
        """
        Return True if the file needs re-indexing:
          - Never been indexed (no row)
          - Content hash changed
          - Embedding model name changed
        """
        row = self._conn.execute(
            "SELECT content_hash, embedding_model FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return True
        stored_hash, stored_model = row
        return stored_hash != content_hash or stored_model != embedding_model

    def known_paths(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT path FROM files").fetchall()}

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def delete_file(self, path: str) -> None:
        """Remove a file record and all its chunks (CASCADE + FTS5 triggers)."""
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def upsert_file(
        self,
        path: str,
        content_hash: str,
        embedding_model: str,
        mtime: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO files(path, content_hash, embedding_model, mtime, indexed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                content_hash    = excluded.content_hash,
                embedding_model = excluded.embedding_model,
                mtime           = excluded.mtime,
                indexed_at      = excluded.indexed_at
            """,
            (path, content_hash, embedding_model, mtime, time.time()),
        )

    def insert_chunks(
        self,
        file_path: str,
        chunks: list[str],
        embeddings: list[list[float]] | None,
    ) -> None:
        """
        Bulk-insert chunks for a file. embeddings must be same length as
        chunks when provided; pass None for BM25-only mode.
        """
        rows = []
        for i, text in enumerate(chunks):
            emb   = embeddings[i] if embeddings is not None else None
            blob  = _vec_to_blob(emb) if emb is not None else None
            rows.append((file_path, i, text, blob))
        self._conn.executemany(
            "INSERT INTO chunks(file_path, chunk_index, text, embedding) VALUES (?,?,?,?)",
            rows,
        )

    def commit(self) -> None:
        self._conn.commit()

    # ------------------------------------------------------------------
    # BM25 search
    # ------------------------------------------------------------------

    def bm25_search(self, query: str, limit: int) -> list[tuple[int, str, str, float]]:
        """
        FTS5 BM25 search. Returns (chunk_id, file_path, text, score) tuples,
        higher score = better match.
        """
        fts_query = self._to_fts_query(query)
        if not fts_query:
            return []
        return self._conn.execute(                          # type: ignore[return-value]
            """
            SELECT c.id, c.file_path, c.text, -bm25(chunks_fts)
            FROM   chunks_fts
            JOIN   chunks c ON chunks_fts.rowid = c.id
            WHERE  chunks_fts MATCH ?
            ORDER  BY bm25(chunks_fts)
            LIMIT  ?
            """,
            (fts_query, limit),
        ).fetchall()

    # ------------------------------------------------------------------
    # Hybrid search (BM25 + cosine)
    # ------------------------------------------------------------------

    def hybrid_search(
        self,
        query: str,
        query_vector: list[float] | None,
        top_k: int,
        bm25_weight: float = 0.3,
        decay_halflife_days: float = 30.0,
        decay_weight: float = 0.0,
    ) -> list[dict]:
        """
        Hybrid BM25 + cosine search with optional temporal decay.
        Falls back to BM25-only when query_vector is None.
        Returns list of {file, path, text, score, mtime} dicts, descending score.

        Temporal decay
        --------------
        When decay_weight > 0, the raw hybrid score is multiplied by a decay
        factor based on the source file's last-modified time (st_mtime stored
        at index time in the files table):

            age_days = (now - file.mtime) / 86400
            decay    = exp(-ln(2) * age_days / decay_halflife_days)
            score    = raw_score * ((1 - decay_weight) + decay_weight * decay)

        At decay_weight=0 (default) the formula is a no-op — existing
        behaviour is fully preserved. At decay_weight=1.0 the score is
        multiplied entirely by the decay factor; values in between blend the
        two. A halflife of 30 means a 30-day-old file scores ~50% of a fresh
        file; 60 days old → ~25%, etc.
        """
        fetch_n   = top_k * 4
        bm25_rows = self.bm25_search(query, fetch_n)

        # BM25-only fallback
        if query_vector is None:
            results = [
                {"file": Path(r[1]).name, "path": r[1], "text": r[2], "score": r[3], "mtime": 0.0}
                for r in bm25_rows[:top_k]
            ]
            if decay_weight > 0:
                results = self._apply_decay(results, decay_halflife_days, decay_weight)
            return results

        # Load all vectors (blobs) for cosine scoring — also fetch mtime via join
        all_blob_rows = self._conn.execute(
            """
            SELECT c.id, c.file_path, c.text, c.embedding
            FROM   chunks c
            WHERE  c.embedding IS NOT NULL
            """
        ).fetchall()

        if not all_blob_rows:
            # No embeddings stored at all — BM25 only
            results = [
                {"file": Path(r[1]).name, "path": r[1], "text": r[2], "score": r[3], "mtime": 0.0}
                for r in bm25_rows[:top_k]
            ]
            if decay_weight > 0:
                results = self._apply_decay(results, decay_halflife_days, decay_weight)
            return results

        # Vectorised cosine for all stored chunks
        vec_scores = _cosine_matrix(query_vector, all_blob_rows)

        # Build candidate pool: union of BM25 hits + all embedded chunks
        fp_text: dict[int, tuple[str, str]] = {r[0]: (r[1], r[2]) for r in all_blob_rows}
        candidates: dict[int, dict] = {}

        for cid, fp, text, bscore in bm25_rows:
            candidates[cid] = {"fp": fp, "text": text, "bm25": bscore, "vec": vec_scores.get(cid, 0.0)}
        for cid, (fp, text) in fp_text.items():
            if cid not in candidates:
                candidates[cid] = {"fp": fp, "text": text, "bm25": 0.0, "vec": vec_scores.get(cid, 0.0)}

        if not candidates:
            return []

        bm25_max = max(v["bm25"] for v in candidates.values()) or 1.0
        vec_max  = max(v["vec"]  for v in candidates.values()) or 1.0
        w_v      = 1.0 - bm25_weight

        ranked = sorted(
            [
                {
                    "file":  Path(info["fp"]).name,
                    "path":  info["fp"],
                    "text":  info["text"],
                    "mtime": 0.0,  # filled by _apply_decay if needed
                    "score": bm25_weight * (info["bm25"] / bm25_max)
                             + w_v * (info["vec"] / vec_max),
                }
                for info in candidates.values()
            ],
            key=lambda x: x["score"],
            reverse=True,
        )
        results = ranked[:top_k]
        if decay_weight > 0:
            results = self._apply_decay(results, decay_halflife_days, decay_weight)
        return results

    def _apply_decay(
        self,
        results: list[dict],
        halflife_days: float,
        decay_weight: float,
    ) -> list[dict]:
        """
        Fetch mtime for each result's source file and apply exponential decay
        to the score. Re-sorts results after rescoring.
        """
        if not results or halflife_days <= 0:
            return results

        now   = time.time()
        paths = list({r["path"] for r in results})
        rows  = self._conn.execute(
            f"SELECT path, mtime FROM files WHERE path IN ({','.join('?' * len(paths))})",
            paths,
        ).fetchall()
        mtime_map: dict[str, float] = {r[0]: r[1] for r in rows}

        out = []
        for r in results:
            mtime    = mtime_map.get(r["path"], now)
            age_days = max(0.0, (now - mtime) / 86400.0)
            decay    = math.exp(-_LN2 * age_days / halflife_days)
            new_score = r["score"] * ((1.0 - decay_weight) + decay_weight * decay)
            out.append({**r, "mtime": mtime, "score": new_score})

        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def total_chunks_text_tokens(self) -> int:
        """
        Return a fast token estimate for ALL chunks in the store.
        Used to skip search entirely when everything already fits in context.
        """
        row = self._conn.execute("SELECT COALESCE(SUM(LENGTH(text)), 0) FROM chunks").fetchone()
        return (row[0] or 0) // 4

    def remove_deleted_files(self, current_paths: set[str]) -> list[str]:
        """Delete index rows for files no longer on disk. Returns removed paths."""
        removed = [p for p in self.known_paths() if p not in current_paths]
        for p in removed:
            self.delete_file(p)
        return removed

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _to_fts_query(query: str) -> str:
        tokens = [t for t in query.split() if t]
        if not tokens:
            return ""
        escaped = [t.replace('"', '""') for t in tokens]
        return " OR ".join(f'"{token}"' for token in escaped)

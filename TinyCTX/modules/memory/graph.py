"""
modules/memory/graph.py

LadybugDB schema, database lifetime management, an in-memory vector index, and
low-level scope-aware graph accessors for the v2 memory system.
(LadybugDB is the community-maintained fork of KùzuDB.)

Schema (v2)
-----------
GraphMeta — key/value metadata; holds `schema_version`.
Entity    — typed knowledge nodes. `scope` and `pinned` use the scope grammar
            (scopes.py). `mention` is a DOUBLE (passive RAG bumps fractionally).
            `created_at` / `updated_at` / `mention` are agent-read-only.
Relation  — directed labelled edges. Hard-deleted on removal (no soft-delete
            column — the old `superseded_at` is gone; tools always DETACH DELETE).

Embeddings
----------
Dimension depends on the configured model (which may change), so the column is
DOUBLE[] (variable length) and cosine is computed in Python. A single embedding
model is used (the old second `graph_*` model is dropped). Staleness is detected
by a SHA-256 content hash: any write that changes name/type/description zeroes
`embed_hash`, marking the row for lazy re-embed. The VectorIndex is an in-memory
matrix cache invalidated by a dirty set the writers populate — we never rescan
the table to detect staleness.

Connection & WAL machinery are carried forward from v1 unchanged in behaviour.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2"

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_DDL = [
    """
    CREATE NODE TABLE IF NOT EXISTS GraphMeta (
        key STRING,
        val STRING,
        PRIMARY KEY (key)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Entity (
        uuid           STRING,
        name           STRING,
        entity_type    STRING,
        description    STRING,
        scope          STRING,
        pinned         STRING,
        mention        DOUBLE,
        created_at     DOUBLE,
        updated_at     DOUBLE,
        embed_hash     STRING,
        embed_content  STRING,
        embedding      DOUBLE[],
        PRIMARY KEY (uuid)
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS Relation (
        FROM Entity TO Entity,
        relation    STRING,
        weight      DOUBLE,
        created_at  DOUBLE,
        updated_at  DOUBLE
    )
    """,
]

# Ladybug's default checkpoint threshold (16 MB) is never reached by a small
# graph, so the WAL would grow forever. Lower it so checkpoints fire often.
_CHECKPOINT_THRESHOLD_BYTES = 500 * 1024  # 500 KB

# Suggested (not enforced) entity types.
ENTITY_TYPES = {
    "Person", "Concept", "Preference", "Fact", "Event", "Location",
    "Organization", "Project", "Technology", "Rule", "Directive", "Role",
}


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def init_schema(conn) -> None:
    """Create tables if absent and stamp the schema version. Idempotent."""
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl.strip())
    try:
        r = conn.execute("MATCH (m:GraphMeta {key: 'schema_version'}) RETURN m.val LIMIT 1")
        if not (r and r.has_next()):
            conn.execute(
                "CREATE (m:GraphMeta {key: 'schema_version', val: $v})",
                parameters={"v": SCHEMA_VERSION},
            )
    except Exception:
        pass
    logger.info("[memory/graph] schema v%s initialised", SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def new_uuid() -> str:
    return str(uuid.uuid4())


def now_ts() -> float:
    return time.time()


class _EmptyResult:
    """Stand-in for a QueryResult with zero rows — see execute_with_retry."""

    def has_next(self) -> bool:
        return False

    def get_next(self):
        raise StopIteration

    def get_column_names(self) -> list:
        return []


async def execute_with_retry(conn, query: str, parameters: dict | None = None):
    """
    Await conn.execute(), tolerating a None return on a freshly-initialised DB
    (ladybug returns None instead of an empty QueryResult for some MATCH queries
    that touch no data — a deterministic "no rows" case, not a transient error).
    """
    result = await conn.execute(query, parameters=parameters) if parameters is not None \
        else await conn.execute(query)
    if result is None:
        return _EmptyResult()
    return result


def embed_hash(content: str) -> str:
    """SHA-256 of the embed content used to detect staleness."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def embed_content_for(name: str, entity_type: str, description: str) -> str:
    """
    Canonical embed string: name, type and description. This is exactly the text
    hashed for `embed_hash`; scope / pin / mention are NOT included because they
    do not change semantics.
    """
    return f"{name} ({entity_type})\n{description}".strip()


# ---------------------------------------------------------------------------
# Cosine similarity (numpy fast path, pure-Python fallback)
# ---------------------------------------------------------------------------

try:
    import numpy as _np  # type: ignore
    _NUMPY = True
except ImportError:
    _NUMPY = False


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    if _NUMPY:
        va = _np.asarray(a, dtype=_np.float64)
        vb = _np.asarray(b, dtype=_np.float64)
        na = _np.linalg.norm(va)
        nb = _np.linalg.norm(vb)
        if na == 0 or nb == 0:
            return 0.0
        return float(_np.dot(va, vb) / (na * nb))
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    if ma == 0 or mb == 0:
        return 0.0
    return dot / (ma * mb)


def top_k_cosine(
    query_vec: list[float],
    rows: list[tuple[str, list[float]]],
    k: int,
) -> list[tuple[str, float]]:
    """Top-k (uuid, score) by cosine, descending; skips empty embeddings."""
    scored = [(uid, cosine_similarity(query_vec, emb)) for uid, emb in rows if emb]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


# ---------------------------------------------------------------------------
# VectorIndex — in-memory matrix cache with dirty-set invalidation
# ---------------------------------------------------------------------------

class VectorIndex:
    """
    Holds `{uuid: embedding}` and answers cosine queries. Invalidation is driven
    by callers: `upsert` when a row is (re)embedded, `remove` on delete. A numpy
    matrix is rebuilt lazily on the next search after any mutation; without numpy
    it falls back to per-row cosine.

    `search` can restrict to an `allowed` uuid set (scope filtering) and applies
    `min_p` BEFORE truncating to `k`, so a low-similarity node never rides a
    small candidate pool into the results.
    """

    def __init__(self) -> None:
        self._vecs: dict[str, list[float]] = {}
        self._dirty = True
        self._matrix = None            # numpy 2D array, rows aligned to _uids
        self._uids: list[str] = []

    def __len__(self) -> int:
        return len(self._vecs)

    def upsert(self, uid: str, vec: list[float]) -> None:
        if vec:
            self._vecs[uid] = list(vec)
            self._dirty = True

    def remove(self, uid: str) -> None:
        if self._vecs.pop(uid, None) is not None:
            self._dirty = True

    def clear(self) -> None:
        self._vecs.clear()
        self._dirty = True

    def _rebuild(self) -> None:
        self._uids = list(self._vecs.keys())
        if _NUMPY and self._uids:
            self._matrix = _np.asarray([self._vecs[u] for u in self._uids], dtype=_np.float64)
        else:
            self._matrix = None
        self._dirty = False

    def search(
        self,
        query_vec: list[float],
        k: int,
        min_p: float = 0.0,
        allowed: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        if not query_vec or not self._vecs:
            return []
        if self._dirty:
            self._rebuild()

        if _NUMPY and self._matrix is not None and self._matrix.shape[0] == len(self._uids):
            q = _np.asarray(query_vec, dtype=_np.float64)
            qn = _np.linalg.norm(q)
            if qn == 0:
                return []
            mat = self._matrix
            norms = _np.linalg.norm(mat, axis=1)
            norms[norms == 0] = 1.0
            sims = (mat @ q) / (norms * qn)
            scored = list(zip(self._uids, (float(s) for s in sims)))
        else:
            scored = [(u, cosine_similarity(query_vec, v)) for u, v in self._vecs.items()]

        if allowed is not None:
            scored = [(u, s) for u, s in scored if u in allowed]
        scored = [(u, s) for u, s in scored if s >= min_p]   # min_p BEFORE top-k
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]


# ---------------------------------------------------------------------------
# WAL-error detection
# ---------------------------------------------------------------------------

def _is_wal_error(exc: BaseException) -> bool:
    msg = " ".join(str(a) for a in getattr(exc, "args", (str(exc),))).lower()
    return ".wal" in msg and ("no such file" in msg or "cannot read" in msg or "error 2" in msg)


# ---------------------------------------------------------------------------
# GraphDatabase — owns the ladybug.Database lifetime + the VectorIndex
# ---------------------------------------------------------------------------

class GraphDatabase:
    """Single owner of the ladybug.Database: open/recover/checkpoint/close, and
    the process-wide VectorIndex."""

    def __init__(self, graph_path: Path, max_concurrent: int = 4) -> None:
        self._graph_path = graph_path
        self._max_concurrent = max_concurrent
        self.vector_index = VectorIndex()

        graph_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = self._open_db(graph_path)
        self._apply_schema()

    @staticmethod
    def _open_db(graph_path: Path):
        import ladybug
        try:
            return ladybug.Database(str(graph_path), checkpoint_threshold=_CHECKPOINT_THRESHOLD_BYTES)
        except Exception as exc:
            logger.warning("[memory/graph] DB open failed (%s) — wiping aux files and retrying", exc)
            parent, stem = graph_path.parent, graph_path.name
            for p in parent.iterdir():
                if p.name.startswith(stem) and p.name != stem:
                    try:
                        p.unlink()
                    except OSError:
                        pass
            return ladybug.Database(str(graph_path), checkpoint_threshold=_CHECKPOINT_THRESHOLD_BYTES)

    def _apply_schema(self) -> None:
        import ladybug
        conn = ladybug.Connection(self._db)
        try:
            init_schema(conn)
        finally:
            conn.close()

    def rebuild(self, stale_write_conn=None) -> None:
        logger.warning("[memory/graph] WAL missing mid-session — rebuilding")
        if stale_write_conn is not None:
            try:
                stale_write_conn.close()
            except Exception:
                pass
        self._db = self._open_db(self._graph_path)
        self._apply_schema()
        logger.info("[memory/graph] database rebuilt after WAL loss")

    def new_read_conn(self):
        import ladybug
        try:
            return ladybug.Connection(self._db)
        except Exception as exc:
            if _is_wal_error(exc):
                self.rebuild()
                return ladybug.Connection(self._db)
            raise

    def new_async_write_conn(self):
        import ladybug
        return ladybug.AsyncConnection(self._db, max_concurrent_queries=self._max_concurrent)

    def checkpoint(self) -> None:
        import ladybug
        try:
            conn = ladybug.Connection(self._db)
            conn.execute("CHECKPOINT")
            conn.close()
        except Exception as exc:
            logger.warning("[memory/graph] checkpoint failed: %s", exc)

    def warm_index(self) -> None:
        """Load already-embedded rows into the VectorIndex on cold start. No
        recompute — only rows whose embedding is present are loaded."""
        try:
            conn = self.new_read_conn()
            r = conn.execute("MATCH (e:Entity) WHERE e.embedding IS NOT NULL RETURN e.uuid, e.embedding")
            n = 0
            while r and r.has_next():
                row = r.get_next()
                if row[1]:
                    self.vector_index.upsert(row[0], row[1])
                    n += 1
            conn.close()
            logger.info("[memory/graph] vector index warmed with %d embeddings", n)
        except Exception as exc:
            logger.warning("[memory/graph] warm_index failed: %s", exc)

    def close(self) -> None:
        self.checkpoint()
        try:
            self._db.close()
        except Exception as exc:
            logger.warning("[memory/graph] db close failed: %s", exc)


# ---------------------------------------------------------------------------
# GraphDB — sync, scope-aware read accessor for the tools
# ---------------------------------------------------------------------------

class GraphDB:
    """Sync read accessor. Every public read takes a `visible` scope set and
    filters `WHERE e.scope IN visible`, so no read path can return an
    out-of-scope node. On a WAL error it rebuilds and retries once."""

    def __init__(self, graph_database: GraphDatabase) -> None:
        self._gdb = graph_database
        self._conn = graph_database.new_read_conn()

    @property
    def vector_index(self) -> VectorIndex:
        return self._gdb.vector_index

    def safe_execute(self, query: str, parameters: dict | None = None) -> Any:
        kwargs: dict = {"parameters": parameters} if parameters else {}
        try:
            return self._conn.execute(query, **kwargs)
        except Exception as exc:
            if _is_wal_error(exc):
                self._gdb.rebuild()
                self._conn = self._gdb.new_read_conn()
                return self._conn.execute(query, **kwargs)
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _rows_to_dicts(result, cols: list[str]) -> list[dict]:
        rows = []
        while result and result.has_next():
            rows.append(dict(zip(cols, result.get_next())))
        return rows

    @staticmethod
    def _scope_ok(scope, visible: set[str] | None) -> bool:
        return visible is None or (scope in visible)

    # -- entity reads (scope-filtered) --------------------------------------

    def get_entity(self, uid: str, visible: set[str] | None = None) -> dict | None:
        r = self.safe_execute("MATCH (e:Entity {uuid: $uid}) RETURN e.*", {"uid": uid})
        if not (r and r.has_next()):
            return None
        row = r.get_next()
        entity = dict(zip(r.get_column_names(), row))
        if not self._scope_ok(entity.get("e.scope"), visible):
            return None
        entity["edges_out"] = self._edges_from(uid, visible)
        entity["edges_in"] = self._edges_to(uid, visible)
        return entity

    def get_entity_slim(self, uid: str, visible: set[str] | None = None) -> dict | None:
        r = self.safe_execute(
            "MATCH (e:Entity {uuid: $uid}) RETURN e.uuid, e.name, e.entity_type, e.description, e.scope",
            {"uid": uid},
        )
        if not (r and r.has_next()):
            return None
        row = r.get_next()
        if not self._scope_ok(row[4], visible):
            return None
        return {"uuid": row[0], "name": row[1], "entity_type": row[2], "description": row[3], "scope": row[4]}

    def find_by_name(self, name: str, visible: set[str] | None = None) -> list[dict]:
        """Substring name match, scope-filtered. Used for exact-match resolution."""
        r = self.safe_execute(
            "MATCH (e:Entity) WHERE e.name =~ $rx "
            "RETURN e.uuid, e.name, e.entity_type, e.description, e.scope LIMIT 25",
            {"rx": f"(?i).*{_regex_escape(name)}.*"},
        )
        out = self._rows_to_dicts(r, ["uuid", "name", "entity_type", "description", "scope"])
        return [e for e in out if self._scope_ok(e["scope"], visible)]

    def name_exists_in_scope(self, name: str, scope: str) -> str | None:
        """Return the uuid of an entity with this exact name in this exact scope,
        or None. Used by memory_add_entity's atomic uniqueness check."""
        r = self.safe_execute(
            "MATCH (e:Entity) WHERE e.name = $n AND e.scope = $s RETURN e.uuid LIMIT 1",
            {"n": name, "s": scope},
        )
        if r and r.has_next():
            return r.get_next()[0]
        return None

    def all_scopes(self) -> set[str]:
        """Every distinct scope present in the graph. Used by the /memory stats
        diagnostic command to show full totals rather than one cycle's view."""
        r = self.safe_execute("MATCH (e:Entity) RETURN DISTINCT e.scope")
        out: set[str] = set()
        while r and r.has_next():
            s = r.get_next()[0]
            if s:
                out.add(s)
        return out

    def scoped_uuids(self, visible: set[str]) -> set[str]:
        """All uuids visible in `visible` — used to constrain vector search."""
        r = self.safe_execute("MATCH (e:Entity) RETURN e.uuid, e.scope")
        out: set[str] = set()
        while r and r.has_next():
            uid, scope = r.get_next()
            if scope in visible:
                out.add(uid)
        return out

    def bm25_corpus(self, visible: set[str]) -> list[tuple[str, str]]:
        """[(uuid, text)] for BM25 over the visible scope only."""
        r = self.safe_execute("MATCH (e:Entity) RETURN e.uuid, e.name, e.entity_type, e.description, e.scope")
        out = []
        while r and r.has_next():
            uid, name, et, desc, scope = r.get_next()
            if scope in visible:
                out.append((uid, f"{name or ''} {et or ''} {desc or ''}"))
        return out

    def pinned_entities(self, visible: set[str]) -> list[dict]:
        """Full dicts (with edges) for pinned entities whose pin target is in the
        visible set, most-recently-updated first."""
        r = self.safe_execute(
            "MATCH (e:Entity) WHERE e.pinned <> '' AND e.pinned IS NOT NULL "
            "RETURN e.uuid, e.pinned ORDER BY e.updated_at DESC"
        )
        results = []
        while r and r.has_next():
            uid, pin = r.get_next()
            if pin in visible:
                ent = self.get_entity(uid, visible)
                if ent:
                    results.append(ent)
        return results

    def get_stats(self, visible: set[str]) -> dict:
        r = self.safe_execute("MATCH (e:Entity) RETURN e.uuid, e.entity_type, e.scope, e.pinned, e.mention, e.embedding")
        by_type: dict[str, int] = {}
        pinned_by_scope: dict[str, int] = {}
        entity_count = embedded = 0
        vis_uuids: set[str] = set()
        while r and r.has_next():
            uid, et, scope, pin, mention, emb = r.get_next()
            if scope not in visible:
                continue
            vis_uuids.add(uid)
            entity_count += 1
            by_type[et] = by_type.get(et, 0) + 1
            if pin:
                pinned_by_scope[pin] = pinned_by_scope.get(pin, 0) + 1
            if emb:
                embedded += 1
        edge_count = self._count_visible_edges(vis_uuids)
        return {
            "entity_count": entity_count,
            "edge_count": edge_count,
            "pinned_by_scope": pinned_by_scope,
            "embedded_count": embedded,
            "by_type": by_type,
        }

    def _count_visible_edges(self, vis_uuids: set[str]) -> int:
        if not vis_uuids:
            return 0
        r = self.safe_execute("MATCH (a:Entity)-[:Relation]->(b:Entity) RETURN a.uuid, b.uuid")
        n = 0
        while r and r.has_next():
            a, b = r.get_next()
            if a in vis_uuids and b in vis_uuids:
                n += 1
        return n

    # -- edge reads (both endpoints must be visible) ------------------------

    def _edges_from(self, uid: str, visible: set[str] | None) -> list[dict]:
        r = self.safe_execute(
            "MATCH (a:Entity {uuid: $uid})-[r:Relation]->(b:Entity) "
            "RETURN b.uuid, b.name, b.scope, r.relation, r.weight",
            {"uid": uid},
        )
        out = []
        while r and r.has_next():
            tgt, tname, tscope, rel, w = r.get_next()
            if self._scope_ok(tscope, visible):
                out.append({"target_uuid": tgt, "target_name": tname, "relation": rel, "weight": w})
        return out

    def _edges_to(self, uid: str, visible: set[str] | None) -> list[dict]:
        r = self.safe_execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity {uuid: $uid}) "
            "RETURN a.uuid, a.name, a.scope, r.relation, r.weight",
            {"uid": uid},
        )
        out = []
        while r and r.has_next():
            src, sname, sscope, rel, w = r.get_next()
            if self._scope_ok(sscope, visible):
                out.append({"source_uuid": src, "source_name": sname, "relation": rel, "weight": w})
        return out


def _regex_escape(s: str) -> str:
    """Escape a string for use inside a Cypher =~ regex literal."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", s)

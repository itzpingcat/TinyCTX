"""
modules/memory/graph.py

LadybugDB schema initialisation, database lifetime management, and low-level
graph access helpers.
(LadybugDB is the community-maintained fork of KùzuDB.)

Schema
------
GraphMeta — key/value store for graph-level metadata (e.g. embedding_dim,
            embed_model name). Used to detect schema/model drift on startup.

Entity    — typed knowledge nodes.
Relation  — directional labelled edges between entities.

Embedding notes
---------------
Ladybug FLOAT[N] arrays support HNSW indexing but require a fixed dimension N
known at schema-creation time. Because the embedding dimension depends on the
configured model (which may change), we use DOUBLE[] (variable-length list)
for the embedding column. Cosine similarity is computed in Python at query
time, exactly as the memory module does. This trades some speed for full
schema flexibility — fine at knowledge-graph scale.

When no embedding model is configured, the embedding column is left NULL on
all nodes and only keyword search is available.

Connection usage
----------------
GraphDatabase owns the single ladybug.Database and is the only place that
opens, checkpoints, or closes it.

The librarian uses an AsyncConnection vended by GraphDatabase for all writes.
The main agent tools use a sync Connection vended by GraphDatabase for reads.

Ladybug supports concurrent readers with one writer via MVCC; both connection
types are created from the same underlying Database object.

Checkpoint behaviour
--------------------
Ladybug's default CHECKPOINT_THRESHOLD is 16 MB, which a small knowledge
graph never reaches, so the WAL would accumulate indefinitely and the main
.lbug files would never be written during normal operation.

We lower the threshold to 500 KB at schema-init time so automatic checkpointing
fires frequently. LibrarianRunner also calls GraphDatabase.checkpoint() via a
done-callback after each agent task completes, ensuring the WAL is flushed
to disk promptly after every write batch.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any as _QueryResult  # placeholder for ladybug QueryResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_DDL = [
    # Graph-level metadata
    """
    CREATE NODE TABLE IF NOT EXISTS GraphMeta (
        key STRING,
        val STRING,
        PRIMARY KEY (key)
    )
    """,

    # Knowledge nodes
    """
    CREATE NODE TABLE IF NOT EXISTS Entity (
        uuid                STRING,
        name                STRING,
        entity_type         STRING,
        description         STRING,
        pinned              BOOL,
        priority            INT64,
        mention_count       INT64,
        created_at          DOUBLE,
        updated_at          DOUBLE,
        embed_model         STRING,
        embed_content       STRING,
        embed_hash          STRING,
        embedding           DOUBLE[],
        graph_embed_model   STRING,
        graph_embed_content STRING,
        graph_embed_hash    STRING,
        graph_embedding     DOUBLE[],
        PRIMARY KEY (uuid)
    )
    """,

    # Relationship edges (single table, all types via 'relation' property)
    """
    CREATE REL TABLE IF NOT EXISTS Relation (
        FROM Entity TO Entity,
        relation     STRING,
        weight       DOUBLE,
        description  STRING,
        created_at   DOUBLE,
        superseded_at DOUBLE
    )
    """,
]

# Checkpoint threshold in bytes. Ladybug's default is 16 MB, which a small
# knowledge graph never reaches. 500 KB ensures the WAL is flushed to the main
# .lbug files after every modest write batch.
_CHECKPOINT_THRESHOLD_BYTES = 500 * 1024  # 500 KB

# ---------------------------------------------------------------------------
# Valid entity types and suggested relation vocabulary
# ---------------------------------------------------------------------------

ENTITY_TYPES = {
    "Person", "Concept", "Preference", "Fact", "Event", "Location",
    "Organization", "Project", "Technology", "Rule", "Directive", "Role",
}


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def init_schema(conn) -> None:
    """
    Create all tables if they don't exist and lower the checkpoint threshold.
    Safe to call on every startup.
    conn may be a sync ladybug.Connection or async ladybug.AsyncConnection —
    callers must await if async (handled externally).
    """
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl.strip())

    logger.info("[memory/graph] schema initialised")



def migrate_schema(conn) -> None:
    """
    Add graph_embedding columns to an existing Entity table that predates them.
    Safe to call on every startup; skips columns that already exist.
    """
    already = False
    try:
        r = conn.execute(
            "MATCH (m:GraphMeta {key: \'migration_graph_embedding_v1\'}) RETURN m.val LIMIT 1"
        )
        already = r.has_next()
    except Exception:
        pass

    if already:
        return

    cols_to_add = [
        ("graph_embed_model",   "STRING"),
        ("graph_embed_content", "STRING"),
        ("graph_embed_hash",    "STRING"),
        ("graph_embedding",     "DOUBLE[]"),
    ]
    for col, dtype in cols_to_add:
        try:
            conn.execute(f"ALTER TABLE Entity ADD {col} {dtype}")
            logger.info("[memory/graph] migration: added column %s %s", col, dtype)
        except Exception as exc:
            if "already exist" in str(exc).lower() or "duplicate" in str(exc).lower():
                logger.debug("[memory/graph] migration: column %s already present", col)
            else:
                logger.warning("[memory/graph] migration: unexpected error adding %s: %s", col, exc)

    try:
        conn.execute(
            "CREATE (m:GraphMeta {key: \'migration_graph_embedding_v1\', val: \'done\'})"
        )
    except Exception:
        pass

    logger.info("[memory/graph] migration: graph_embedding columns ready")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def new_uuid() -> str:
    return str(uuid.uuid4())


def now_ts() -> float:
    return time.time()


def embed_hash(content: str) -> str:
    """SHA-256 of the content string used to detect embedding staleness."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def embed_content_for(name: str, description: str) -> str:
    """The string we embed — name + space + description."""
    return f"{name} {description}"


def graph_embed_content_for(name: str, entity_type: str, description: str) -> str:
    """
    The string we embed for graph/dedup purposes.
    Includes entity_type so the model can distinguish e.g. two entities named
    'Alice' of different types.
    """
    return f"{entity_type}: {name} {description}"


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    import numpy as _np
try:
    import numpy as _np  # type: ignore[no-redef]
    _NUMPY = True
except ImportError:
    _NUMPY = False


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    if _NUMPY:
        va = _np.array(a, dtype=_np.float64)
        vb = _np.array(b, dtype=_np.float64)
        na = _np.linalg.norm(va)
        nb = _np.linalg.norm(vb)
        if na == 0 or nb == 0:
            return 0.0
        return float(_np.dot(va, vb) / (na * nb))
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def top_k_cosine(
    query_vec: list[float],
    rows: list[tuple[str, list[float]]],  # [(uuid, embedding), ...]
    k: int,
) -> list[tuple[str, float]]:
    """
    Return top-k (uuid, score) pairs by cosine similarity, descending.
    Skips rows with null/empty embeddings.
    """
    scored = []
    for uid, emb in rows:
        if not emb:
            continue
        score = cosine_similarity(query_vec, emb)
        scored.append((uid, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


# ---------------------------------------------------------------------------
# WAL-error detection
# ---------------------------------------------------------------------------

def _is_wal_error(exc: BaseException) -> bool:
    """
    Return True when *exc* looks like the 'Cannot read size of file … .wal'
    IO error that ladybug raises when the WAL has been deleted mid-session.
    """
    msg = " ".join(str(a) for a in getattr(exc, "args", (str(exc),))).lower()
    return ".wal" in msg and ("no such file" in msg or "cannot read" in msg or "error 2" in msg)


# ---------------------------------------------------------------------------
# GraphDatabase — owns the ladybug.Database lifetime
# ---------------------------------------------------------------------------

class GraphDatabase:
    """
    Owns the single ladybug.Database for the memory module.

    Responsibilities:
      - Open (or recover) the database on construction
      - Checkpoint and close cleanly on shutdown
      - Rebuild mid-session if a WAL error is detected
      - Vend sync and async connections to callers

    All ladybug imports are local so the module can be imported without
    ladybug installed (e.g. for type checking or testing stubs).
    """

    def __init__(self, graph_path: Path, max_concurrent: int = 4) -> None:
        self._graph_path    = graph_path
        self._max_concurrent = max_concurrent

        graph_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = self._open_db(graph_path)

        # Apply schema immediately so callers get a ready-to-use database.
        self._apply_schema()

    # ------------------------------------------------------------------
    # Open / recover
    # ------------------------------------------------------------------

    @staticmethod
    def _open_db(graph_path: Path):
        """
        Open (or create) a ladybug.Database at *graph_path*.

        On failure, wipe auxiliary files (including any stale .wal) adjacent
        to the DB file and retry once with a fresh database.
        """
        import ladybug

        try:
            return ladybug.Database(str(graph_path), checkpoint_threshold=_CHECKPOINT_THRESHOLD_BYTES)
        except Exception as exc:
            logger.warning(
                "[memory/graph] DB failed to open (%s) — wiping auxiliary files and retrying",
                exc,
            )
            parent = graph_path.parent
            stem   = graph_path.name
            for p in parent.iterdir():
                if p.name.startswith(stem) and p.name != stem:
                    try:
                        p.unlink()
                        logger.info("[memory/graph] deleted stale file %s", p)
                    except OSError as del_exc:
                        logger.warning("[memory/graph] could not delete %s: %s", p, del_exc)
            return ladybug.Database(str(graph_path), checkpoint_threshold=_CHECKPOINT_THRESHOLD_BYTES)

    def _apply_schema(self) -> None:
        """Run schema DDL and configuration on a fresh sync connection, then close it."""
        import ladybug
        conn = ladybug.Connection(self._db)
        try:
            init_schema(conn)
            migrate_schema(conn)
        finally:
            conn.close()

    def rebuild(self, stale_write_conn=None) -> None:
        """
        Rebuild the database after a mid-session WAL error.

        Closes *stale_write_conn* if provided (best-effort), wipes auxiliary
        files, reopens the database, and re-applies the schema.  The caller
        must discard any connections obtained before this call and request
        fresh ones via new_read_conn() / new_async_write_conn().
        """
        logger.warning("[memory/graph] WAL missing mid-session — rebuilding database")

        if stale_write_conn is not None:
            try:
                stale_write_conn.close()
            except Exception:
                pass

        self._db = self._open_db(self._graph_path)
        self._apply_schema()
        logger.info("[memory/graph] database rebuilt successfully after WAL loss")

    # ------------------------------------------------------------------
    # Connection factories
    # ------------------------------------------------------------------

    def new_read_conn(self):
        """
        Return a new sync ladybug.Connection for read operations.

        If a WAL error fires on open, rebuild the database first and retry.
        """
        import ladybug

        try:
            return ladybug.Connection(self._db)
        except Exception as exc:
            if _is_wal_error(exc):
                logger.warning(
                    "[memory/graph] new_read_conn: WAL error (%s) — rebuilding", exc
                )
                self.rebuild()
                return ladybug.Connection(self._db)
            raise

    def new_async_write_conn(self):
        """
        Return a new ladybug.AsyncConnection for write operations (librarian).
        """
        import ladybug

        return ladybug.AsyncConnection(
            self._db,
            max_concurrent_queries=self._max_concurrent,
        )

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def checkpoint(self) -> None:
        """
        Flush the WAL into the main database files.

        Opens a fresh sync connection so there is no active transaction
        conflict. Safe to call at any time; logs a warning on failure rather
        than raising (checkpoint is best-effort outside of shutdown).
        """
        import ladybug

        try:
            conn = ladybug.Connection(self._db)
            conn.execute("CHECKPOINT")
            conn.close()
            logger.debug("[memory/graph] checkpoint complete")
        except Exception as exc:
            logger.warning("[memory/graph] checkpoint failed: %s", exc)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Checkpoint then close the database."""
        self.checkpoint()
        try:
            self._db.close()
        except Exception as exc:
            logger.warning("[memory/graph] db close failed: %s", exc)


# ---------------------------------------------------------------------------
# GraphDB — sync read accessor for the main agent tools
# ---------------------------------------------------------------------------

class GraphDB:
    """
    Sync graph accessor for use in the main agent's tool implementations.

    Holds a sync ladybug.Connection obtained from a GraphDatabase.  On a
    mid-session WAL error, safe_execute asks the owning GraphDatabase to
    rebuild and then opens a fresh connection automatically.
    """

    def __init__(self, graph_database: GraphDatabase) -> None:
        self._gdb  = graph_database
        self._conn = graph_database.new_read_conn()

    # ------------------------------------------------------------------
    # Safe execute — retries once after a mid-session WAL rebuild
    # ------------------------------------------------------------------

    def safe_execute(self, query: str, parameters: dict | None = None) -> Any:
        """
        Execute *query* on the current connection.

        On a WAL-missing error the GraphDatabase is rebuilt, a fresh
        connection is opened, and the query is retried once.  Any other
        exception propagates normally.
        """
        kwargs: dict = {}
        if parameters:
            kwargs["parameters"] = parameters

        try:
            return self._conn.execute(query, **kwargs)
        except Exception as exc:
            if _is_wal_error(exc):
                logger.warning(
                    "[memory/graph] WAL error on read query — rebuilding: %s", exc
                )
                try:
                    self._gdb.rebuild()
                    self._conn = self._gdb.new_read_conn()
                    logger.info("[memory/graph] connection rebuilt; retrying query")
                    return self._conn.execute(query, **kwargs)
                except Exception as retry_exc:
                    logger.error(
                        "[memory/graph] query still failing after rebuild: %s", retry_exc
                    )
                    raise retry_exc from exc
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_entity(self, uid: str) -> dict | None:
        r = self.safe_execute(
            "MATCH (e:Entity {uuid: $uid}) RETURN e.*",
            parameters={"uid": uid},
        )
        if not r.has_next():
            return None
        row = r.get_next()
        col_names = r.get_column_names()
        entity = dict(zip(col_names, row))

        entity["edges_out"] = self._active_edges_from(uid)
        entity["edges_in"]  = self._active_edges_to(uid)
        return entity

    def find_entity(self, name: str | None = None, entity_type: str | None = None) -> list[dict]:
        if name and entity_type:
            r = self.safe_execute(
                "MATCH (e:Entity) WHERE e.name CONTAINS $name AND e.entity_type = $et RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
                parameters={"name": name, "et": entity_type},
            )
        elif name:
            r = self.safe_execute(
                "MATCH (e:Entity) WHERE e.name CONTAINS $name RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
                parameters={"name": name},
            )
        elif entity_type:
            r = self.safe_execute(
                "MATCH (e:Entity) WHERE e.entity_type = $et RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
                parameters={"et": entity_type},
            )
        else:
            return []
        return self._rows_to_dicts(r, ["uuid", "name", "entity_type", "description"])

    def list_entities(self, entity_type: str | None = None, pinned_only: bool = False) -> list[dict]:
        clauses = []
        params: dict[str, Any] = {}
        if entity_type:
            clauses.append("e.entity_type = $et")
            params["et"] = entity_type
        if pinned_only:
            clauses.append("e.pinned = true")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        r = self.safe_execute(
            f"MATCH (e:Entity) {where} RETURN e.uuid, e.name, e.entity_type, e.description, e.pinned, e.priority ORDER BY e.priority DESC",
            parameters=params if params else None,
        )
        return self._rows_to_dicts(r, ["uuid", "name", "entity_type", "description", "pinned", "priority"])

    def get_pinned_entities(self) -> list[dict]:
        r = self.safe_execute(
            "MATCH (e:Entity) WHERE e.pinned = true RETURN e.uuid, e.name, e.entity_type, e.description ORDER BY e.priority DESC"
        )
        return self._rows_to_dicts(r, ["uuid", "name", "entity_type", "description"])

    def get_pinned_entities_full(self) -> list[dict]:
        """
        Return full entity dicts (including edges) for all pinned entities,
        ordered by priority descending. Used by the memory block assembler.
        """
        r = self.safe_execute(
            "MATCH (e:Entity) WHERE e.pinned = true RETURN e.uuid ORDER BY e.priority DESC"
        )
        uuids = [row[0] for row in self._drain(r)]
        results = []
        for uid in uuids:
            entity = self.get_entity(uid)
            if entity:
                results.append(entity)
        return results

    def get_entity_slim(self, uid: str) -> dict | None:
        """Fetch name/type/description only — no edges. Used for linked-node rendering."""
        r = self.safe_execute(
            "MATCH (e:Entity {uuid: $uid}) "
            "RETURN e.uuid, e.name, e.entity_type, e.description",
            parameters={"uid": uid},
        )
        if not r.has_next():
            return None
        row = r.get_next()
        return {"uuid": row[0], "name": row[1], "entity_type": row[2], "description": row[3]}

    def traverse(
        self,
        uid: str,
        hops: int = 1,
        relation_filter: str | None = None,
    ) -> list[dict]:
        """Walk outward from uid up to N hops, active edges only."""
        visited: set[str] = {uid}
        frontier: set[str] = {uid}
        all_edges: list[dict] = []

        for _ in range(hops):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for src in frontier:
                for edge in self._active_edges_from(src):
                    tgt = edge["target_uuid"]
                    if relation_filter and edge["relation"] != relation_filter:
                        continue
                    if tgt not in visited:
                        next_frontier.add(tgt)
                        visited.add(tgt)
                    all_edges.append(edge)
            frontier = next_frontier

        return all_edges

    def get_stats(self) -> dict:
        entity_count = self.safe_execute(
            "MATCH (e:Entity) RETURN count(e)"
        ).get_next()[0]

        edge_count = self.safe_execute(
            "MATCH ()-[r:Relation]->() WHERE r.superseded_at IS NULL RETURN count(r)"
        ).get_next()[0]

        superseded_edge_count = self.safe_execute(
            "MATCH ()-[r:Relation]->() WHERE r.superseded_at IS NOT NULL RETURN count(r)"
        ).get_next()[0]

        pinned_count = self.safe_execute(
            "MATCH (e:Entity) WHERE e.pinned = true RETURN count(e)"
        ).get_next()[0]

        avg_priority_row = self.safe_execute(
            "MATCH (e:Entity) RETURN avg(e.priority)"
        ).get_next()[0]
        avg_priority = round(float(avg_priority_row), 1) if avg_priority_row is not None else 0.0

        embedded_count = self.safe_execute(
            "MATCH (e:Entity) WHERE e.embedding IS NOT NULL RETURN count(e)"
        ).get_next()[0]

        r = self.safe_execute(
            "MATCH (e:Entity) RETURN e.entity_type, count(e) ORDER BY count(e) DESC"
        )
        by_type: dict[str, int] = {}
        while r.has_next():
            row = r.get_next()
            by_type[row[0]] = row[1]

        r2 = self.safe_execute(
            "MATCH (e:Entity) WHERE e.mention_count > 0 "
            "RETURN e.name, e.entity_type, e.mention_count "
            "ORDER BY e.mention_count DESC LIMIT 5"
        )
        top_mentioned: list[dict] = []
        while r2.has_next():
            row = r2.get_next()
            top_mentioned.append({"name": row[0], "entity_type": row[1], "mention_count": row[2]})

        return {
            "entity_count":           entity_count,
            "active_edge_count":      edge_count,
            "superseded_edge_count":  superseded_edge_count,
            "pinned_count":           pinned_count,
            "avg_priority":           avg_priority,
            "embedded_count":         embedded_count,
            "by_type":                by_type,
            "top_mentioned":          top_mentioned,
        }

    def all_entities_with_embeddings(self) -> list[tuple[str, list[float]]]:
        """Return [(uuid, embedding)] for all entities that have embeddings."""
        r = self.safe_execute(
            "MATCH (e:Entity) WHERE e.embedding IS NOT NULL RETURN e.uuid, e.embedding"
        )
        results = []
        while r.has_next():
            row = r.get_next()
            emb = row[1]
            if emb:
                results.append((row[0], emb))
        return results

    def all_entities_with_graph_embeddings(self) -> list[tuple[str, list[float]]]:
        """
        Return [(uuid, embedding)] for dedup, preferring graph_embedding.
        Falls back to the regular search embedding when graph_embedding is NULL.
        """
        r = self.safe_execute(
            "MATCH (e:Entity) RETURN e.uuid, e.graph_embedding, e.embedding"
        )
        results = []
        while r.has_next():
            row = r.get_next()
            uid, graph_emb, search_emb = row[0], row[1], row[2]
            emb = graph_emb if graph_emb else search_emb
            if emb:
                results.append((uid, emb))
        return results

    def bump_mention_count(self, uids: list[str]) -> None:
        for uid in uids:
            self.safe_execute(
                "MATCH (e:Entity {uuid: $uid}) SET e.mention_count = e.mention_count + 1",
                parameters={"uid": uid},
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _active_edges_from(self, uid: str) -> list[dict]:
        r = self.safe_execute(
            "MATCH (a:Entity {uuid: $uid})-[r:Relation]->(b:Entity) "
            "WHERE r.superseded_at IS NULL "
            "RETURN b.uuid, b.name, r.relation, r.weight, r.description",
            parameters={"uid": uid},
        )
        return self._rows_to_dicts(r, ["target_uuid", "target_name", "relation", "weight", "description"])

    def _active_edges_to(self, uid: str) -> list[dict]:
        r = self.safe_execute(
            "MATCH (a:Entity)-[r:Relation]->(b:Entity {uuid: $uid}) "
            "WHERE r.superseded_at IS NULL "
            "RETURN a.uuid, a.name, r.relation, r.weight, r.description",
            parameters={"uid": uid},
        )
        return self._rows_to_dicts(r, ["source_uuid", "source_name", "relation", "weight", "description"])

    @staticmethod
    def _rows_to_dicts(result, col_names: list[str]) -> list[dict]:
        rows = []
        while result.has_next():
            rows.append(dict(zip(col_names, result.get_next())))
        return rows

    @staticmethod
    def _drain(result) -> list:
        rows = []
        while result.has_next():
            rows.append(result.get_next())
        return rows

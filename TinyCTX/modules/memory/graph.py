"""
modules/memory/graph.py

LadybugDB schema initialisation and low-level graph access helpers.
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
The librarian process uses ladybug.AsyncConnection (shared, writer).
The main agent tools use ladybug.Connection (sync, reader — ladybug supports
concurrent readers with one writer via its MVCC).

Both callers open their own ladybug.Database handle; ladybug handles
coordination at the storage layer.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid
from typing import Any

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
        uuid         STRING,
        name         STRING,
        entity_type  STRING,
        description  STRING,
        pinned       BOOL,
        priority     INT64,
        mention_count INT64,
        created_at   DOUBLE,
        updated_at   DOUBLE,
        embed_model  STRING,
        embed_content STRING,
        embed_hash   STRING,
        embedding    DOUBLE[],
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
    Create all tables if they don't exist. Safe to call on every startup.
    conn may be a sync ladybug.Connection or async ladybug.AsyncConnection —
    callers must await if async (handled externally).
    """
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl.strip())
    logger.info("[memory/graph] schema initialised")


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


# ---------------------------------------------------------------------------
# Cosine similarity (same pattern as memory/store.py)
# ---------------------------------------------------------------------------

try:
    import numpy as _np
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
# GraphDB — thin wrapper around a shared Connection for the main agent (sync)
# ---------------------------------------------------------------------------

class GraphDB:
    """
    Sync graph accessor for use in the main agent's tool implementations.
    Receives a ladybug.Connection created from the single shared Database object
    owned by the LibrarianRunner. Does NOT open its own Database.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_entity(self, uid: str) -> dict | None:
        r = self._conn.execute(
            "MATCH (e:Entity {uuid: $uid}) RETURN e.*",
            parameters={"uid": uid},
        )
        if not r.has_next():
            return None
        row = r.get_next()
        col_names = r.get_column_names()
        entity = dict(zip(col_names, row))

        # Attach active in/out edges
        entity["edges_out"] = self._active_edges_from(uid)
        entity["edges_in"]  = self._active_edges_to(uid)
        return entity

    def find_entity(self, name: str | None = None, entity_type: str | None = None) -> list[dict]:
        if name and entity_type:
            r = self._conn.execute(
                "MATCH (e:Entity) WHERE e.name CONTAINS $name AND e.entity_type = $et RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
                parameters={"name": name, "et": entity_type},
            )
        elif name:
            r = self._conn.execute(
                "MATCH (e:Entity) WHERE e.name CONTAINS $name RETURN e.uuid, e.name, e.entity_type, e.description LIMIT 10",
                parameters={"name": name},
            )
        elif entity_type:
            r = self._conn.execute(
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
        r = self._conn.execute(
            f"MATCH (e:Entity) {where} RETURN e.uuid, e.name, e.entity_type, e.description, e.pinned, e.priority ORDER BY e.priority DESC",
            parameters=params,
        )
        return self._rows_to_dicts(r, ["uuid", "name", "entity_type", "description", "pinned", "priority"])

    def get_pinned_entities(self) -> list[dict]:
        r = self._conn.execute(
            "MATCH (e:Entity) WHERE e.pinned = true RETURN e.uuid, e.name, e.entity_type, e.description ORDER BY e.priority DESC"
        )
        return self._rows_to_dicts(r, ["uuid", "name", "entity_type", "description"])

    def get_pinned_entities_full(self) -> list[dict]:
        """
        Return full entity dicts (including edges) for all pinned entities,
        ordered by priority descending. Used by the memory block assembler.
        """
        r = self._conn.execute(
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
        r = self._conn.execute(
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
        entity_count = self._conn.execute("MATCH (e:Entity) RETURN count(e)").get_next()[0]
        edge_count   = self._conn.execute(
            "MATCH ()-[r:Relation]->() WHERE r.superseded_at IS NULL RETURN count(r)"
        ).get_next()[0]
        r = self._conn.execute(
            "MATCH (e:Entity) RETURN e.entity_type, count(e) ORDER BY count(e) DESC"
        )
        by_type: dict[str, int] = {}
        while r.has_next():
            row = r.get_next()
            by_type[row[0]] = row[1]
        return {
            "entity_count": entity_count,
            "active_edge_count": edge_count,
            "by_type": by_type,
        }

    def all_entities_with_embeddings(self) -> list[tuple[str, list[float]]]:
        """Return [(uuid, embedding)] for all entities that have embeddings."""
        r = self._conn.execute(
            "MATCH (e:Entity) WHERE e.embedding IS NOT NULL RETURN e.uuid, e.embedding"
        )
        results = []
        while r.has_next():
            row = r.get_next()
            emb = row[1]
            if emb:
                results.append((row[0], emb))
        return results

    def bump_mention_count(self, uids: list[str]) -> None:
        for uid in uids:
            self._conn.execute(
                "MATCH (e:Entity {uuid: $uid}) SET e.mention_count = e.mention_count + 1",
                parameters={"uid": uid},
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _active_edges_from(self, uid: str) -> list[dict]:
        r = self._conn.execute(
            "MATCH (a:Entity {uuid: $uid})-[r:Relation]->(b:Entity) "
            "WHERE r.superseded_at IS NULL "
            "RETURN b.uuid, b.name, r.relation, r.weight, r.description",
            parameters={"uid": uid},
        )
        return self._rows_to_dicts(r, ["target_uuid", "target_name", "relation", "weight", "description"])

    def _active_edges_to(self, uid: str) -> list[dict]:
        r = self._conn.execute(
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

    def close(self) -> None:
        pass  # connection lifetime managed by LibrarianRunner

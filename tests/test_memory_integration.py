"""
Live-DB integration tests for the v2 memory subsystem.

These exercise the REAL ladybug graph end-to-end (schema, scope-filtered reads,
atomic uniqueness, relation conflict deletion, merge, description-diff edits,
vector search, and v1->v2 migration). They are skipped automatically wherever
ladybug is not importable (e.g. this authoring sandbox, or CI without the
engine), and run in full on a machine with ladybug + Python 3.14.

Run:   pytest tests/test_memory_integration.py -v

pytest-asyncio is required (asyncio_mode = auto is set in pytest.ini).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("ladybug", reason="ladybug engine not installed")

from TinyCTX.modules.memory import tools
from TinyCTX.modules.memory.graph import GraphDatabase, GraphDB


# ---------------------------------------------------------------------------
# Fixture: a real, isolated graph wired into the tools module
# ---------------------------------------------------------------------------

class FakeEmbedder:
    """Deterministic 3-dim embedder so vector search is testable without a model.
    Maps a keyword to a basis vector; unknown text -> zero-ish vector."""

    _MAP = {
        "atlas": [1.0, 0.0, 0.0],
        "roadmap": [0.96, 0.28, 0.0],   # close to atlas
        "pizza": [0.0, 1.0, 0.0],       # orthogonal
    }

    async def embed_one(self, text: str, priority: int = 10):
        t = text.lower()
        for k, v in self._MAP.items():
            if k in t:
                return list(v)
        return [0.0, 0.0, 1.0]


def _make_graph(tmp_path, embedder=None, cfg=None):
    graph_path = tmp_path / "memory" / "memory.lbug"
    gdbase = GraphDatabase(graph_path)
    write_conn = gdbase.new_async_write_conn()
    write_lock = asyncio.Lock()
    graph_db = GraphDB(gdbase)
    base_cfg = {"bm25_weight": 0.4, "rrf_k": 60, "search_min_p": 0.0, "passive_min_p": 0.0,
                "embed_query_template": "{text}", "embed_document_template": "{text}"}
    if cfg:
        base_cfg.update(cfg)
    tools.init(write_conn, write_lock, graph_db, embedder, cfg=base_cfg, data_dir=tmp_path)
    return gdbase, graph_db


@pytest.fixture
def graph(tmp_path):
    gdbase, graph_db = _make_graph(tmp_path)
    yield gdbase, graph_db
    graph_db.close()
    gdbase.close()


# ---------------------------------------------------------------------------
# Add / read-back / atomic uniqueness / scope
# ---------------------------------------------------------------------------

async def test_add_and_search_roundtrip(graph):
    with tools.scope_context({"global"}):
        out = await tools.memory_add_entity("Project Atlas", "Project", "the shared roadmap", "global")
        assert "Added" in out
        found = await tools.search_memory("Atlas", top_k=5)
    assert "Project Atlas" in found


async def test_atomic_unique_name_in_scope(graph):
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("Bob", "Person", "first", "global")
        second = await tools.memory_add_entity("Bob", "Person", "second", "global")
    assert "already exists" in second
    assert "first" in second  # existing entity's data is returned on collision


async def test_same_name_distinct_scopes_allowed(tmp_path):
    gdbase, graph_db = _make_graph(tmp_path)
    try:
        with tools.scope_context({"global", "user:able"}):
            a = await tools.memory_add_entity("Notes", "Fact", "global notes", "global")
            b = await tools.memory_add_entity("Notes", "Fact", "able's notes", "user:able")
        assert "Added" in a and "Added" in b
    finally:
        graph_db.close()
        gdbase.close()


async def test_scope_isolation_end_to_end(tmp_path):
    gdbase, graph_db = _make_graph(tmp_path)
    try:
        # seed nodes in three scopes
        with tools.scope_context({"global", "user:able", "user:carl"}):
            await tools.memory_add_entity("Global Fact", "Fact", "atlas visible to all", "global")
            await tools.memory_add_entity("Able Fact", "Fact", "atlas able secret", "user:able")
            await tools.memory_add_entity("Carl Fact", "Fact", "atlas carl secret", "user:carl")
        # Able's view excludes Carl
        with tools.scope_context({"global", "user:able"}):
            out = await tools.search_memory("atlas", top_k=10)
        assert "Global Fact" in out and "Able Fact" in out and "Carl Fact" not in out
        # exact-match on an out-of-scope node returns nothing
        with tools.scope_context({"global", "user:able"}):
            exact = await tools.search_memory("Carl Fact", top_k=5)
        assert "Carl Fact" not in exact
    finally:
        graph_db.close()
        gdbase.close()


# ---------------------------------------------------------------------------
# Relationships: conflict deletion, weight update, directionality
# ---------------------------------------------------------------------------

async def test_relation_conflict_group_deletion(graph):
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("A", "Concept", "a", "global")
        await tools.memory_add_entity("B", "Concept", "b", "global")
        await tools.memory_set_relationship("A", "B", "DEPENDS_ON", 0.5)
        out = await tools.memory_set_relationship("A", "B", "SUPERSEDES", 0.9)
        a = await tools.search_memory("A", top_k=1)
    assert "removed conflicting" in out
    assert "SUPERSEDES" in a and "DEPENDS_ON" not in a


async def test_relation_weight_update_not_duplicated(graph):
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("A", "Concept", "a", "global")
        await tools.memory_add_entity("B", "Concept", "b", "global")
        await tools.memory_set_relationship("A", "B", "RELATED_TO", 0.3)
        out = await tools.memory_set_relationship("A", "B", "RELATED_TO", 0.8)
        a = await tools.search_memory("A", top_k=1)
    assert "Updated" in out
    assert a.count("RELATED_TO") == 1 and "w=0.8" in a


async def test_delete_relationship_directional(graph):
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("A", "Concept", "a", "global")
        await tools.memory_add_entity("B", "Concept", "b", "global")
        await tools.memory_set_relationship("A", "B", "KNOWS", 0.5)
        await tools.memory_set_relationship("B", "A", "KNOWS", 0.5)
        await tools.memory_delete_relationship("A", "B", "KNOWS")
        a = await tools.search_memory("A", top_k=1)
    # A->B deleted, B->A survives (shows as incoming on A)
    assert "->[KNOWS]->" not in a and "<-[KNOWS]<-" in a


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

async def test_merge_duplicate_reparents_and_deletes(graph):
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("Canon", "Person", "canonical", "global")
        await tools.memory_add_entity("Dup", "Person", "duplicate", "global")
        await tools.memory_add_entity("Friend", "Person", "friend", "global")
        await tools.memory_set_relationship("Dup", "Friend", "KNOWS", 0.7)
        out = await tools.memory_merge_into("Canon", "Dup", "merged", "duplicate")
        canon = await tools.search_memory("Canon", top_k=1)
        gone = await tools.search_memory("Dup", top_k=3)
    assert "Merged" in out
    assert "->[KNOWS]-> Friend" in canon      # edge reparented
    assert "Dup" not in gone                    # duplicate deleted


async def test_merge_alias_keeps_both(graph):
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("Robert", "Person", "full name", "global")
        await tools.memory_add_entity("Bob", "Person", "nickname", "global")
        out = await tools.memory_merge_into("Robert", "Bob", "Robert aka Bob", "alias")
        bob = await tools.search_memory("Bob", top_k=1)
    assert "Aliased" in out
    assert "ALIASED_TO" in bob


# ---------------------------------------------------------------------------
# Description diff + embedding staleness
# ---------------------------------------------------------------------------

async def test_update_description_diff_and_embed_stale(graph):
    gdbase, graph_db = graph
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("Doc", "Concept", "line one\nline two\nline three", "global")
        diff = "@@ -1,3 +1,3 @@\n line one\n-line two\n+LINE TWO EDITED\n line three"
        out = await tools.memory_update_entity_description("Doc", diff)
        found = await tools.search_memory("Doc", top_k=1)
    assert "Updated description" in out
    assert "LINE TWO EDITED" in found
    # embed_hash zeroed => marked stale
    r = graph_db.safe_execute("MATCH (e:Entity {name:'Doc'}) RETURN e.embed_hash")
    assert r.get_next()[0] in ("", None)


async def test_update_description_stale_base_rejected(graph):
    with tools.scope_context({"global"}):
        await tools.memory_add_entity("Doc2", "Concept", "actual content here", "global")
        diff = "@@ -1,1 +1,1 @@\n-totally different base\n+new"
        out = await tools.memory_update_entity_description("Doc2", diff)
    assert "did not apply" in out.lower()


# ---------------------------------------------------------------------------
# Vector search with a deterministic embedder + edge visibility
# ---------------------------------------------------------------------------

async def test_vector_search_and_embedding_pass(tmp_path):
    from TinyCTX.modules.memory import deduper
    gdbase, graph_db = _make_graph(tmp_path, embedder=FakeEmbedder(),
                                   cfg={"similarity_threshold": 0.9})
    try:
        with tools.scope_context({"global"}):
            await tools.memory_add_entity("Atlas", "Project", "the atlas roadmap", "global")
            await tools.memory_add_entity("Pizza", "Concept", "pizza topping notes", "global")
        # run the embedding pass to populate embeddings + the vector index
        n = await deduper.refresh_embeddings(tools._cfg, tools._conn, tools._write_lock,
                                             FakeEmbedder(), graph_db)
        assert n == 2 and len(graph_db.vector_index) == 2
        # a query semantically near "atlas roadmap" should surface Atlas
        with tools.scope_context({"global"}):
            out = await tools.search_memory("roadmap", top_k=1)
        assert "Atlas" in out
    finally:
        graph_db.close()
        gdbase.close()


async def test_edge_visibility_requires_both_endpoints(tmp_path):
    gdbase, graph_db = _make_graph(tmp_path)
    try:
        with tools.scope_context({"global", "user:able"}):
            await tools.memory_add_entity("Shared", "Project", "global project", "global")
            await tools.memory_add_entity("AbleNote", "Fact", "able private note", "user:able")
            await tools.memory_set_relationship("Shared", "AbleNote", "RELATED_TO", 0.5)
        # Bill (no user:able) sees Shared but NOT the edge to the invisible AbleNote
        with tools.scope_context({"global", "user:bill"}):
            out = await tools.search_memory("Shared", top_k=1)
        assert "Shared" in out and "AbleNote" not in out
    finally:
        graph_db.close()
        gdbase.close()


# ---------------------------------------------------------------------------
# v1 -> v2 migration fidelity (builds a real v1-shaped graph)
# ---------------------------------------------------------------------------

def test_migration_fidelity(tmp_path):
    import ladybug
    from TinyCTX.modules.memory import migrate as mig

    old_path = tmp_path / "memory" / "graph.lbug"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    db = ladybug.Database(str(old_path))
    conn = ladybug.Connection(db)
    conn.execute(
        "CREATE NODE TABLE Entity (uuid STRING, name STRING, entity_type STRING, "
        "description STRING, pinned_target STRING, priority INT64, mention_count INT64, "
        "created_at DOUBLE, updated_at DOUBLE, embed_hash STRING, embedding DOUBLE[], "
        "PRIMARY KEY (uuid))"
    )
    conn.execute(
        "CREATE REL TABLE Relation (FROM Entity TO Entity, relation STRING, weight DOUBLE, "
        "created_at DOUBLE, superseded_at DOUBLE)"
    )
    conn.execute("CREATE (e:Entity {uuid:'1', name:'Alice', entity_type:'Person', "
                 "description:'a person', pinned_target:'alice', priority:40, mention_count:3, "
                 "created_at:1.0, updated_at:2.0, embed_hash:'stale'})")
    conn.execute("CREATE (e:Entity {uuid:'2', name:'Bob', entity_type:'Person', "
                 "description:'another', pinned_target:'global', priority:50, mention_count:0, "
                 "created_at:1.0, updated_at:2.0, embed_hash:''})")
    conn.execute("MATCH (a:Entity {uuid:'1'}), (b:Entity {uuid:'2'}) "
                 "CREATE (a)-[:Relation {relation:'KNOWS', weight:0.5, created_at:1.0, superseded_at:null}]->(b)")
    conn.execute("MATCH (a:Entity {uuid:'1'}), (b:Entity {uuid:'2'}) "
                 "CREATE (a)-[:Relation {relation:'OLD', weight:0.1, created_at:1.0, superseded_at:9.0}]->(b)")
    conn.close()
    db.close()

    new_path = tmp_path / "memory" / "memory.lbug"
    summary = mig.migrate(old_path, new_path)
    assert summary["status"] == "migrated"
    assert summary["entities_out"] == 2
    assert summary["edges_out"] == 1 and summary["edges_dropped"] == 1  # superseded edge dropped
    assert "backup" in summary and not old_path.exists()               # renamed, not deleted

    # verify v2 fields
    gdbase = GraphDatabase(new_path)
    graph_db = GraphDB(gdbase)
    try:
        with tools.scope_context({"global"}):
            tools.init(gdbase.new_async_write_conn(), asyncio.Lock(), graph_db, None,
                       cfg={}, data_dir=tmp_path)
        r = graph_db.safe_execute("MATCH (e:Entity {uuid:'1'}) RETURN e.scope, e.pinned, e.mention")
        scope, pinned, mention = r.get_next()
        assert scope == "global"          # everything -> global
        assert pinned == "user:alice"     # pinned_target mapped to grammar
        assert mention == 3.0
        r2 = graph_db.safe_execute("MATCH (e:Entity {uuid:'2'}) RETURN e.pinned")
        assert r2.get_next()[0] == "global"
    finally:
        graph_db.close()
        gdbase.close()

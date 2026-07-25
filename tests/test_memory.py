"""
Tests for the v2 memory subsystem.

The live graph engine (ladybug) is not importable in CI and the package targets
Python 3.14, so these tests cover the pure logic and the scope-filtering read
paths via an in-memory FakeGraphDB. Items that require the real DB engine
(atomic CREATE uniqueness under a live write lock, WAL rebuild) are covered by
the pure uniqueness/scoping logic here and must additionally be smoke-tested on a
ladybug + py3.14 environment before release.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from TinyCTX.modules.memory import scopes
from TinyCTX.modules.memory import tools
from TinyCTX.modules.memory.graph import VectorIndex, embed_content_for, embed_hash
from TinyCTX.modules.memory import deduper
from TinyCTX.modules.memory import migrate
from TinyCTX.modules.memory import reviewer
from TinyCTX.modules.memory.flaggers import decay_candidate, fuzzy_names


# ---------------------------------------------------------------------------
# Scope grammar + resolution
# ---------------------------------------------------------------------------

def test_scope_grammar():
    assert scopes.is_valid_scope("global")
    assert scopes.is_valid_scope("user:bob")
    assert scopes.is_valid_scope("guild:my_server")
    assert not scopes.is_valid_scope("bad scope")
    assert not scopes.is_valid_scope("")
    assert not scopes.is_valid_scope("User:Bob")  # kind must be lowercase


def test_resolve_scopes_isolation():
    visible = scopes.resolve_scopes({"server_name": "Server 1"}, {"able", "bill"})
    assert visible == {"global", "guild:server_1", "user:able", "user:bill"}
    assert "user:carl" not in visible


# ---------------------------------------------------------------------------
# Relation vocab + conflict groups
# ---------------------------------------------------------------------------

def test_relation_conflict_groups():
    tools._load_relations()
    assert tools._CONFLICT_GROUPS["SUPERSEDES"] == {"DEPENDS_ON", "CONFLICTS_WITH"}
    assert tools._CONFLICT_GROUPS["LIKES"] == {"DISLIKES"}
    assert "KNOWS" not in tools._CONFLICT_GROUPS  # standalone
    assert tools._valid_relation("SUPERSEDES")
    assert not tools._valid_relation("bad rel")


# ---------------------------------------------------------------------------
# RRF fusion + unified diff + throttle
# ---------------------------------------------------------------------------

def test_rrf_fusion_prefers_dual_hits():
    fused = dict(tools._rrf_fuse({"a": 1, "b": 2}, {"b": 1, "c": 3}))
    assert max(fused, key=fused.get) == "b"  # in both retrievers


def test_unified_diff_apply_stale_and_malformed():
    base = "line one\nline two\nline three"
    diff = "@@ -1,3 +1,3 @@\n line one\n-line two\n+LINE TWO\n line three"
    ok, out = tools._apply_unified_diff(base, diff)
    assert ok and out == "line one\nLINE TWO\nline three"
    assert tools._apply_unified_diff("different\ntext", diff)[0] is False  # stale base
    assert tools._apply_unified_diff(base, "no hunks")[0] is False          # malformed


def test_throttle_scales_with_queue():
    assert tools.throttle_delay(1, base=30, min_delay=2, target=10) == 30    # short -> spaced
    assert tools.throttle_delay(100, base=30, min_delay=2, target=10) == 3.0  # long -> fast
    assert tools.throttle_delay(0, base=30, min_delay=2, target=10) == 30


# ---------------------------------------------------------------------------
# VectorIndex: min-p before top-k, scope restriction, invalidation
# ---------------------------------------------------------------------------

def test_vector_index_min_p_and_scope():
    vi = VectorIndex()
    vi.upsert("a", [1, 0, 0])
    vi.upsert("b", [0, 1, 0])
    vi.upsert("c", [0.95, 0.31, 0])
    # min-p drops the orthogonal 'b' even though pool is tiny
    hits = dict(vi.search([1, 0, 0], k=5, min_p=0.5))
    assert "b" not in hits and "a" in hits and "c" in hits
    # scope restriction
    scoped = dict(vi.search([1, 0, 0], k=5, min_p=0.0, allowed={"b"}))
    assert set(scoped) == {"b"}


def test_vector_index_invalidation():
    vi = VectorIndex()
    vi.upsert("a", [1, 0])
    assert len(vi) == 1
    vi.remove("a")
    assert len(vi) == 0 and vi.search([1, 0], k=3) == []


# ---------------------------------------------------------------------------
# Deduper pure algorithms
# ---------------------------------------------------------------------------

def test_clique_edge_cover_covers_all_edges():
    pairs = [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e")]
    groups = deduper.clique_edge_cover(pairs, max_size=8)
    covered = set()
    for g in groups:
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                covered.add(tuple(sorted((g[i], g[j]))))
    assert all(tuple(sorted(p)) in covered for p in pairs)
    assert all(len(g) <= 8 for g in groups)


def test_candidate_pairs_and_parse_ops():
    vecs = {"a": [1, 0], "b": [0.99, 0.01], "c": [0, 1]}
    assert deduper.candidate_pairs(vecs, 0.9) == [("a", "b")]
    ops = deduper.parse_merge_ops('x [{"canonical":"A","duplicate":"B","verdict":"duplicate"}] y')
    assert ops == [{"canonical": "A", "duplicate": "B", "merged_description": "", "verdict": "duplicate"}]
    assert deduper.parse_merge_ops("no json") == []


# ---------------------------------------------------------------------------
# Migration mapping
# ---------------------------------------------------------------------------

def test_migration_mapping():
    old = {"uuid": "u1", "name": "Bob", "entity_type": "Person", "description": "A person",
           "pinned_target": "alice", "priority": 40, "mention_count": 7,
           "created_at": 1.0, "updated_at": 2.0, "embedding": [0.1], "embed_hash": "stale"}
    m = migrate.map_entity(old)
    assert m["scope"] == "global"          # everything -> global
    assert m["pinned"] == "user:alice"     # pinned_target -> pinned grammar
    assert m["mention"] == 7.0
    assert "priority" not in m             # dropped
    assert m["embed_hash"] == "" and m["embedding"] is None  # stale hash -> lazy re-embed
    # embedding preserved when hash matches
    content = embed_content_for("Bob", "Person", "A person")
    old2 = {**old, "embed_hash": embed_hash(content), "embedding": [0.5]}
    assert migrate.map_entity(old2)["embedding"] == [0.5]
    assert migrate.map_pinned("global") == "global"
    assert migrate.map_pinned(None) == ""
    assert migrate.should_skip_edge(5.0) and not migrate.should_skip_edge(None)


# ---------------------------------------------------------------------------
# Reviewer queue: dedup + durability across reload + front-push
# ---------------------------------------------------------------------------

def test_reviewer_queue_dedup_and_durability(tmp_path):
    async def run():
        qpath = tmp_path / "reviewer_queue.json"
        q = reviewer.ReviewerQueue(qpath)
        i1 = {"flagger_type": "orphaned", "entity_uuids": ["b", "a"], "scope": "global", "detail": ""}
        i1b = {"flagger_type": "orphaned", "entity_uuids": ["a", "b"], "scope": "global", "detail": "dup"}
        i2 = {"flagger_type": "decay_candidate", "entity_uuids": ["c"], "scope": "global", "detail": ""}
        added = await q.append_deduped([i1, i1b, i2])   # i1b is a dup of i1 (order-insensitive)
        assert added == 2
        assert q.counts_by_type() == {"orphaned": 1, "decay_candidate": 1}
        # durability: reload from disk
        q2 = reviewer.ReviewerQueue(qpath)
        assert len(q2) == 2
        # front push jumps the line
        await q2.push_front({"flagger_type": "manual", "entity_uuids": [], "scope": "global", "detail": "x"})
        assert (await q2.pop())["flagger_type"] == "manual"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Decay-as-flagger + fuzzy names (pure parts)
# ---------------------------------------------------------------------------

def test_effective_mention_half_life():
    import time
    now = time.time()
    assert round(decay_candidate.effective_mention(4.0, now, 30, now), 2) == 4.0
    assert round(decay_candidate.effective_mention(4.0, now - 30 * 86400, 30, now), 2) == 2.0


def test_fuzzy_similar_pairs():
    ents = [{"name": "Kamie", "uuid": "1", "scope": "global"},
            {"name": "Kamiee", "uuid": "2", "scope": "global"},
            {"name": "Bob", "uuid": "3", "scope": "global"}]
    pairs = fuzzy_names.similar_name_pairs(ents, 80)
    assert len(pairs) == 1 and {pairs[0][0]["name"], pairs[0][1]["name"]} == {"Kamie", "Kamiee"}


# ---------------------------------------------------------------------------
# Scope-filtered hybrid search via FakeGraphDB (no ladybug)
# ---------------------------------------------------------------------------

class _FakeVecIndex(VectorIndex):
    pass


class FakeGraphDB:
    """Minimal in-memory GraphDB implementing the read surface search_memory and
    the passive block use. Enforces scope filtering exactly like the real one."""

    def __init__(self, entities):
        # entities: list of dicts uuid,name,entity_type,description,scope,pinned,embedding
        self._e = {e["uuid"]: e for e in entities}
        self.vector_index = VectorIndex()
        for e in entities:
            if e.get("embedding"):
                self.vector_index.upsert(e["uuid"], e["embedding"])

    def get_entity_slim(self, uid, visible=None):
        e = self._e.get(uid)
        if not e or (visible is not None and e["scope"] not in visible):
            return None
        return {k: e[k] for k in ("uuid", "name", "entity_type", "description", "scope")}

    def find_by_name(self, name, visible=None):
        out = []
        for e in self._e.values():
            if name.lower() in e["name"].lower() and (visible is None or e["scope"] in visible):
                out.append({k: e[k] for k in ("uuid", "name", "entity_type", "description", "scope")})
        return out

    def bm25_corpus(self, visible):
        return [(e["uuid"], f"{e['name']} {e['entity_type']} {e['description']}")
                for e in self._e.values() if e["scope"] in visible]

    def scoped_uuids(self, visible):
        return {u for u, e in self._e.items() if e["scope"] in visible}

    def get_entity(self, uid, visible=None):
        e = self._e.get(uid)
        if not e or (visible is not None and e["scope"] not in visible):
            return None
        d = {f"e.{k}": e[k] for k in ("uuid", "name", "entity_type", "description", "scope", "pinned")}
        d["edges_out"] = []
        d["edges_in"] = []
        return d

    def pinned_entities(self, visible):
        out = []
        for e in self._e.values():
            if e.get("pinned") and e["pinned"] in visible:
                out.append(self.get_entity(e["uuid"], visible))
        return out

    def safe_execute(self, *a, **k):
        class _R:
            def has_next(self_):
                return False
            def get_next(self_):
                raise StopIteration
        return _R()


def _install_fake(monkeypatch, entities, embedder=None):
    fake = FakeGraphDB(entities)
    monkeypatch.setattr(tools, "_graph_db", fake)
    monkeypatch.setattr(tools, "_embedder", embedder)
    monkeypatch.setattr(tools, "_cfg", {"bm25_weight": 0.4, "rrf_k": 60, "search_min_p": 0.0,
                                        "embed_query_template": "{text}"})
    tools._load_relations()
    return fake


def test_search_memory_scope_isolation(monkeypatch):
    entities = [
        {"uuid": "g", "name": "Project Atlas", "entity_type": "Project", "description": "shared roadmap", "scope": "global", "pinned": ""},
        {"uuid": "a", "name": "Able secret", "entity_type": "Fact", "description": "atlas note able", "scope": "user:able", "pinned": ""},
        {"uuid": "c", "name": "Carl secret", "entity_type": "Fact", "description": "atlas note carl", "scope": "user:carl", "pinned": ""},
    ]
    _install_fake(monkeypatch, entities, embedder=None)
    visible = {"global", "user:able"}  # Carl not present

    async def run():
        with tools.scope_context(visible):
            out = await tools.search_memory("atlas", top_k=10)
        return out

    out = asyncio.run(run())
    assert "Project Atlas" in out
    assert "Able secret" in out
    assert "Carl secret" not in out     # out-of-scope node never returned


def test_search_memory_exact_match_respects_scope(monkeypatch):
    entities = [
        {"uuid": "c", "name": "Carl secret", "entity_type": "Fact", "description": "x", "scope": "user:carl", "pinned": ""},
    ]
    _install_fake(monkeypatch, entities)

    async def run():
        with tools.scope_context({"global", "user:able"}):
            return await tools.search_memory("Carl secret", top_k=5)

    assert "not found" in asyncio.run(run()).lower() or "no matching" in asyncio.run(run()).lower()

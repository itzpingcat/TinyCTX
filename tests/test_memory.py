"""
tests/test_memory.py

Test suite for modules/memory: chunkers, store, indexer, and the
_format_results budget trimmer.

All tests are fully offline — no LLM, no embedding server, no real agent.
The embedder is faked with a simple async stub that returns deterministic
vectors. SQLite and file I/O use pytest's tmp_path fixture so nothing
persists between tests.

Run with:
    pytest tests/test_memory.py -v
"""
from __future__ import annotations

import asyncio
import math
import struct
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# chunkers
# ---------------------------------------------------------------------------

from modules.memory.chunkers import (
    CharChunker,
    DelimiterChunker,
    MarkdownChunker,
    TokenChunker,
    available_strategies,
    get_strategy,
)


class TestTokenChunker:
    def test_basic_split(self):
        chunker = TokenChunker(chunk_tokens=3, overlap_tokens=0)
        result = chunker.chunk("a b c d e f")
        assert result == ["a b c", "d e f"]

    def test_overlap(self):
        chunker = TokenChunker(chunk_tokens=4, overlap_tokens=2)
        words = "a b c d e f g h"
        result = chunker.chunk(words)
        # Each chunk should share 2 words with the previous
        assert result[0][:3] == "a b"
        assert result[1].startswith("c d")

    def test_empty_text(self):
        assert TokenChunker().chunk("") == []

    def test_single_chunk(self):
        chunker = TokenChunker(chunk_tokens=100)
        result = chunker.chunk("hello world")
        assert result == ["hello world"]

    def test_no_empty_chunks(self):
        chunker = TokenChunker(chunk_tokens=2, overlap_tokens=0)
        result = chunker.chunk("  a  b  c  ")
        assert all(c.strip() for c in result)


class TestCharChunker:
    def test_basic_split(self):
        chunker = CharChunker(chunk_size=5, overlap=0)
        result = chunker.chunk("abcdefghij")
        assert result == ["abcde", "fghij"]

    def test_overlap(self):
        chunker = CharChunker(chunk_size=6, overlap=2)
        result = chunker.chunk("abcdefghij")
        assert result[0] == "abcdef"
        assert result[1] == "efghij"

    def test_empty_text(self):
        assert CharChunker().chunk("") == []

    def test_shorter_than_chunk(self):
        chunker = CharChunker(chunk_size=100)
        assert CharChunker(chunk_size=100).chunk("hello") == ["hello"]


class TestDelimiterChunker:
    def test_double_newline(self):
        text = "para one\n\npara two\n\npara three"
        result = DelimiterChunker().chunk(text)
        assert result == ["para one", "para two", "para three"]

    def test_custom_delimiter(self):
        chunker = DelimiterChunker(delimiter="---")
        result = chunker.chunk("a---b---c")
        assert result == ["a", "b", "c"]

    def test_empty_parts_stripped(self):
        chunker = DelimiterChunker(delimiter="\n\n")
        result = chunker.chunk("\n\nhello\n\n\n\nworld\n\n")
        assert "hello" in result
        assert "world" in result
        assert all(r.strip() for r in result)

    def test_empty_text(self):
        assert DelimiterChunker().chunk("") == []


class TestMarkdownChunker:
    def test_splits_on_headings(self):
        text = "# Title\n\nIntro text.\n\n## Section\n\nBody text."
        result = MarkdownChunker(min_chunk_chars=0).chunk(text)
        assert any("Title" in c for c in result)
        assert any("Section" in c for c in result)

    def test_merges_tiny_chunks(self):
        # A heading with very short content should merge with next chunk
        text = "# A\n\nhi\n\n# B\n\n" + "x" * 200
        result = MarkdownChunker(min_chunk_chars=50).chunk(text)
        # The tiny "# A\n\nhi" block should be merged forward
        assert all(len(c) >= 10 for c in result)

    def test_splits_on_hr(self):
        text = "above\n\n---\n\nbelow"
        result = MarkdownChunker(min_chunk_chars=0).chunk(text)
        assert len(result) >= 2

    def test_empty_text(self):
        assert MarkdownChunker().chunk("") == []

    def test_no_empty_chunks(self):
        text = "# H1\n\n## H2\n\n### H3\n\nsome content here that is long enough"
        result = MarkdownChunker(min_chunk_chars=0).chunk(text)
        assert all(c.strip() for c in result)


class TestGetStrategy:
    def test_all_strategies_loadable(self):
        for name in available_strategies():
            s = get_strategy(name)
            assert callable(s.chunk)

    def test_kwargs_override(self):
        s = get_strategy("tokens", chunk_tokens=512)
        assert s.chunk_tokens == 512

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown chunk strategy"):
            get_strategy("nonexistent")


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

from modules.memory.store import MemoryStore, _cosine_matrix, _vec_to_blob, _blob_to_vec


class TestVecSerialization:
    def test_roundtrip(self):
        vec = [0.1, 0.2, 0.3, 0.4]
        assert _blob_to_vec(_vec_to_blob(vec)) == pytest.approx(vec, abs=1e-5)

    def test_zero_vector(self):
        vec = [0.0, 0.0, 0.0]
        assert _blob_to_vec(_vec_to_blob(vec)) == pytest.approx(vec)

    def test_blob_length(self):
        vec = [1.0] * 8
        blob = _vec_to_blob(vec)
        assert len(blob) == 8 * 4  # 4 bytes per float32


class TestCosineMatrix:
    def _rows(self, vecs):
        """Build fake rows in the format _cosine_matrix expects."""
        return [(i, "fp", "text", _vec_to_blob(v)) for i, v in enumerate(vecs)]

    def test_identical_vector(self):
        q = [1.0, 0.0, 0.0]
        rows = self._rows([[1.0, 0.0, 0.0]])
        scores = _cosine_matrix(q, rows)
        assert scores[0] == pytest.approx(1.0, abs=1e-5)

    def test_orthogonal_vectors(self):
        q = [1.0, 0.0]
        rows = self._rows([[0.0, 1.0]])
        scores = _cosine_matrix(q, rows)
        assert scores[0] == pytest.approx(0.0, abs=1e-5)

    def test_opposite_vector(self):
        q = [1.0, 0.0]
        rows = self._rows([[-1.0, 0.0]])
        scores = _cosine_matrix(q, rows)
        assert scores[0] == pytest.approx(-1.0, abs=1e-5)

    def test_zero_query(self):
        q = [0.0, 0.0]
        rows = self._rows([[1.0, 0.0]])
        scores = _cosine_matrix(q, rows)
        assert scores[0] == 0.0

    def test_empty_rows(self):
        assert _cosine_matrix([1.0, 0.0], []) == {}

    def test_multiple_rows(self):
        q    = [1.0, 0.0]
        rows = self._rows([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
        scores = _cosine_matrix(q, rows)
        assert scores[0] > scores[2] > scores[1]


class TestMemoryStore:
    def test_empty_store_nothing_dirty(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        # Unknown path → always dirty
        assert store.is_dirty("/some/file.md", "abc", "model-x") is True

    def test_upsert_then_not_dirty(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "hash1", "model-x", 0.0)
        store.commit()
        assert store.is_dirty("/f.md", "hash1", "model-x") is False

    def test_dirty_on_hash_change(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "hash1", "model-x", 0.0)
        store.commit()
        assert store.is_dirty("/f.md", "hash2", "model-x") is True

    def test_dirty_on_model_change(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "hash1", "model-x", 0.0)
        store.commit()
        assert store.is_dirty("/f.md", "hash1", "model-y") is True

    def test_known_paths(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/a.md", "h", "m", 0.0)
        store.upsert_file("/b.md", "h", "m", 0.0)
        store.commit()
        assert store.known_paths() == {"/a.md", "/b.md"}

    def test_delete_file_removes_chunks(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "h", "m", 0.0)
        store.insert_chunks("/f.md", ["chunk a", "chunk b"], None)
        store.commit()
        store.delete_file("/f.md")
        store.commit()
        assert "/f.md" not in store.known_paths()
        # FTS5 should be empty too
        assert store.bm25_search("chunk", 10) == []

    def test_bm25_search_basic(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "h", "m", 0.0)
        store.insert_chunks("/f.md", ["the quick brown fox", "lazy dog jumps"], None)
        store.commit()
        results = store.bm25_search("fox", 5)
        assert len(results) == 1
        assert "fox" in results[0][2]

    def test_bm25_search_escapes_quotes_in_query(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "h", "m", 0.0)
        store.insert_chunks("/f.md", ['hello "world" entry'], None)
        store.commit()
        results = store.bm25_search('hello "world', 5)
        assert len(results) == 1
        assert 'hello "world" entry' == results[0][2]

    def test_bm25_empty_query_returns_nothing(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        assert store.bm25_search("", 5) == []

    def test_insert_chunks_with_embeddings(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "h", "m", 0.0)
        vecs = [[1.0, 0.0], [0.0, 1.0]]
        store.insert_chunks("/f.md", ["text a", "text b"], vecs)
        store.commit()
        blob_rows = store._conn.execute(
            "SELECT embedding FROM chunks WHERE file_path = '/f.md'"
        ).fetchall()
        assert all(r[0] is not None for r in blob_rows)

    def test_hybrid_search_bm25_only_fallback(self, tmp_path):
        """With no embeddings stored, hybrid_search falls back to BM25."""
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "h", "m", 0.0)
        store.insert_chunks("/f.md", ["the quick fox", "a lazy dog"], None)
        store.commit()
        results = store.hybrid_search("fox", None, top_k=5)
        assert len(results) == 1
        assert results[0]["text"] == "the quick fox"

    def test_hybrid_search_with_vector(self, tmp_path):
        """Hybrid scoring returns results even when BM25 has no match."""
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/f.md", "h", "m", 0.0)
        # Two chunks with known vectors; query matches chunk 0 by cosine
        vecs = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        store.insert_chunks("/f.md", ["chunk zero", "chunk one"], vecs)
        store.commit()
        query_vec = [1.0, 0.0, 0.0]
        results = store.hybrid_search("zzz_no_bm25_match", query_vec, top_k=5, bm25_weight=0.0)
        # bm25_weight=0 → pure vector, chunk 0 should score highest
        assert results[0]["text"] == "chunk zero"

    def test_remove_deleted_files(self, tmp_path):
        store = MemoryStore(tmp_path / "cache.db")
        store.upsert_file("/a.md", "h", "m", 0.0)
        store.upsert_file("/b.md", "h", "m", 0.0)
        store.commit()
        removed = store.remove_deleted_files({"/a.md"})
        store.commit()
        assert removed == ["/b.md"]
        assert store.known_paths() == {"/a.md"}

    def test_migration_from_json_text(self, tmp_path):
        """Old TEXT/JSON embeddings are automatically converted to BLOBs."""
        import json, sqlite3
        db_path = tmp_path / "cache.db"

        # Create an old-style DB with TEXT embedding column
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                embedding_model TEXT NOT NULL DEFAULT '',
                mtime REAL NOT NULL DEFAULT 0,
                indexed_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                text, content='chunks', content_rowid='id'
            );
            CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
            END;
        """)
        vec = [0.1, 0.2, 0.3]
        conn.execute(
            "INSERT INTO files VALUES ('/f.md','h','m',0,0)"
        )
        conn.execute(
            "INSERT INTO chunks(file_path, chunk_index, text, embedding) VALUES (?,?,?,?)",
            ("/f.md", 0, "hello", json.dumps(vec)),
        )
        conn.commit()
        conn.close()

        # Opening via MemoryStore should auto-migrate
        store = MemoryStore(db_path)
        blob_row = store._conn.execute(
            "SELECT embedding FROM chunks WHERE file_path = '/f.md'"
        ).fetchone()
        assert isinstance(blob_row[0], bytes)
        recovered = _blob_to_vec(blob_row[0])
        assert recovered == pytest.approx(vec, abs=1e-5)


# ---------------------------------------------------------------------------
# indexer
# ---------------------------------------------------------------------------

from modules.memory.indexer import MemoryIndexer
from modules.memory.chunkers import get_strategy
from modules.memory.store import MemoryStore


class _FakeEmbedder:
    """
    Deterministic fake embedder. Returns a 3-D unit vector derived from
    the hash of each input string — unique per text, no HTTP calls.
    """
    def __init__(self):
        self.model = "fake-embed"
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self._vec(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    @staticmethod
    def _vec(text: str) -> list[float]:
        h = hash(text) & 0xFFFFFF
        x = ((h >> 16) & 0xFF) / 255.0
        y = ((h >> 8)  & 0xFF) / 255.0
        z = (h         & 0xFF) / 255.0
        mag = math.sqrt(x*x + y*y + z*z) or 1.0
        return [x/mag, y/mag, z/mag]


def _make_indexer(tmp_path, embedder=None, strategy_name="chars"):
    store      = MemoryStore(tmp_path / "cache.db")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    strategy   = get_strategy(strategy_name, chunk_size=200, overlap=0)
    model_str  = embedder.model if embedder else ""
    return store, memory_dir, MemoryIndexer(
        store=store,
        memory_dir=memory_dir,
        strategy=strategy,
        embedder=embedder,
        embedding_model=model_str,
    )


class TestMemoryIndexer:
    async def test_sync_empty_dir(self, tmp_path):
        store, _, indexer = _make_indexer(tmp_path)
        await indexer.sync()
        assert store.known_paths() == set()

    async def test_new_file_is_indexed(self, tmp_path):
        store, memory_dir, indexer = _make_indexer(tmp_path)
        (memory_dir / "notes.md").write_text("hello world " * 20)
        await indexer.sync()
        assert any("notes.md" in p for p in store.known_paths())

    async def test_unchanged_file_not_reindexed(self, tmp_path):
        embedder = _FakeEmbedder()
        store, memory_dir, indexer = _make_indexer(tmp_path, embedder)
        (memory_dir / "notes.md").write_text("hello world " * 20)
        await indexer.sync()
        first_call_count = len(embedder.calls)
        await indexer.sync()
        # No new embedding calls — file wasn't dirty
        assert len(embedder.calls) == first_call_count

    async def test_changed_file_reindexed(self, tmp_path):
        embedder = _FakeEmbedder()
        store, memory_dir, indexer = _make_indexer(tmp_path, embedder)
        f = memory_dir / "notes.md"
        f.write_text("version one " * 20)
        await indexer.sync()
        f.write_text("version two " * 20)
        await indexer.sync()
        # Should have embedded twice
        assert len(embedder.calls) == 2

    async def test_deleted_file_removed_from_store(self, tmp_path):
        store, memory_dir, indexer = _make_indexer(tmp_path)
        f = memory_dir / "gone.md"
        f.write_text("some content " * 10)
        await indexer.sync()
        assert any("gone.md" in p for p in store.known_paths())
        f.unlink()
        await indexer.sync()
        assert not any("gone.md" in p for p in store.known_paths())

    async def test_nested_files_indexed(self, tmp_path):
        store, memory_dir, indexer = _make_indexer(tmp_path)
        sub = memory_dir / "subdir"
        sub.mkdir()
        (sub / "deep.md").write_text("deep content " * 10)
        await indexer.sync()
        assert any("deep.md" in p for p in store.known_paths())

    async def test_embedder_called_with_chunks(self, tmp_path):
        embedder = _FakeEmbedder()
        store, memory_dir, indexer = _make_indexer(tmp_path, embedder)
        # Write enough text to produce multiple chunks (chunk_size=200)
        (memory_dir / "big.md").write_text("x " * 500)
        await indexer.sync()
        assert len(embedder.calls) > 0
        assert all(isinstance(t, str) for batch in embedder.calls for t in batch)

    async def test_embedding_failure_stores_without_vectors(self, tmp_path):
        class _FailEmbedder(_FakeEmbedder):
            async def embed(self, texts):
                raise RuntimeError("server down")

        store, memory_dir, indexer = _make_indexer(tmp_path, _FailEmbedder())
        (memory_dir / "notes.md").write_text("hello " * 30)
        await indexer.sync()   # should not raise
        # File should still be indexed, just without embeddings
        assert any("notes.md" in p for p in store.known_paths())
        blob_rows = store._conn.execute(
            "SELECT embedding FROM chunks"
        ).fetchall()
        assert all(r[0] is None for r in blob_rows)

    async def test_model_change_triggers_reindex(self, tmp_path):
        """Swapping the embedding model should cause all files to be re-indexed."""
        store      = MemoryStore(tmp_path / "cache.db")
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        strategy   = get_strategy("chars", chunk_size=200, overlap=0)
        (memory_dir / "notes.md").write_text("hello " * 40)

        emb1 = _FakeEmbedder()
        emb1.model = "model-v1"
        idx1 = MemoryIndexer(store, memory_dir, strategy, emb1, "model-v1")
        await idx1.sync()
        assert len(emb1.calls) == 1

        # Same store, same file, but different model name
        emb2 = _FakeEmbedder()
        emb2.model = "model-v2"
        idx2 = MemoryIndexer(store, memory_dir, strategy, emb2, "model-v2")
        await idx2.sync()
        assert len(emb2.calls) == 1  # re-indexed because model changed


# ---------------------------------------------------------------------------
# Context nudge
# ---------------------------------------------------------------------------

from context import Context, HistoryEntry, HOOK_PRE_ASSEMBLE_ASYNC


class _FakeAgentConfig:
    """Minimal config stub that register() needs to set up the nudge hook."""

    class _WorkspaceConfig:
        path = "/tmp/tinyctx_nudge_test"

    class _LLMConfig:
        primary  = "default"
        fallback = []

        class _FallbackOn:
            any_error  = False
            http_codes = []

        fallback_on = _FallbackOn()

    workspace = _WorkspaceConfig()
    llm       = _LLMConfig()
    context   = 10_000  # 10k token limit

    def __init__(self, nudge_threshold=0.8, nudge_message=None):
        self.extra = {
            "memory_search": {
                # Disable everything we don't care about for nudge tests
                "embedding_model":    "",
                "auto_inject":        False,
                "nudge_threshold":    nudge_threshold,
                "nudge_message":      nudge_message or (
                    "Context is getting full. Write to memory/session-{date}.md."
                ),
                # Point files somewhere that won't exist (providers return None)
                "soul_file":          "/nonexistent/SOUL.md",
                "agents_file":        "/nonexistent/AGENTS.md",
                "memory_file":        "/nonexistent/MEMORY.md",
                "memory_dir":         "/nonexistent/memory",
                "db_file":            "/tmp/nudge_test_cache.db",
                "chunk_strategy":     "chars",
                "top_k":              3,
                "bm25_weight":        0.5,
                "memory_budget_tokens": 256,
            }
        }
        self.models = {}


class _FakeToolHandler:
    def register_tool(self, fn, always_on=False, **kwargs): pass
    def get_tool_definitions(self):    return []
    def enable(self, name):            return True


class _FakeAgent:
    """Minimal agent stub — just enough for register() to run."""
    def __init__(self, cfg):
        self.config       = cfg
        self.context      = Context(token_limit=cfg.context)
        self.tool_handler = _FakeToolHandler()
        self._queued_background_branches: list[str] = []
        self._background_hooks: list = []

    def queue_background_branch(self, tail_node_id: str) -> None:
        """Capture background branch requests instead of firing real asyncio tasks."""
        self._queued_background_branches.append(tail_node_id)

    def register_background_hook(self, fn) -> None:
        """Capture background hooks; memory module's nudge hook wires here."""
        self._background_hooks.append(fn)


def _make_agent(nudge_threshold=0.8, nudge_message=None):
    cfg   = _FakeAgentConfig(nudge_threshold=nudge_threshold, nudge_message=nudge_message)
    agent = _FakeAgent(cfg)
    # register() wires all hooks including _nudge_hook
    from modules.memory.__main__ import register
    register(agent)
    return agent


# ---------------------------------------------------------------------------
# Helpers for DB-backed nudge tests
# ---------------------------------------------------------------------------

def _make_agent_with_db(tmp_path, nudge_threshold=0.8, nudge_message=None):
    """Like _make_agent() but wires a real ConversationDB into the context."""
    from db import ConversationDB
    cfg   = _FakeAgentConfig(nudge_threshold=nudge_threshold, nudge_message=nudge_message)
    agent = _FakeAgent(cfg)
    db    = ConversationDB(tmp_path / "agent.db")
    root  = db.get_root()
    session_node = db.add_node(parent_id=root.id, role="system", content="session:nudge-test")
    agent.context.set_db(db)
    agent.context.set_tail(session_node.id)
    from modules.memory.__main__ import register
    register(agent)
    return agent, db


async def _fire_nudge_hook(agent, tail_node_id="fake-tail"):
    """Call the consolidation hook registered by memory.register()."""
    assert agent._background_hooks, "No background hooks registered — is nudge_threshold > 0?"
    await agent._background_hooks[0](tail_node_id, agent.config)


class TestContextNudge:
    """
    Tests for the delta-based context nudge injected by modules/memory.

    Phase 3: the nudge is now a *background hook* (registered via
    register_background_hook), not a HOOK_PRE_ASSEMBLE_ASYNC hook.
    In the no-DB path it falls back to inline ctx.dialogue injection for
    backwards compat; in the DB-backed path it queues a background branch.

    Tests invoke _fire_nudge_hook() to simulate what agent.run() does after
    AgentTextFinal: calling each registered background hook with the current
    tail_node_id and config.
    """

    async def test_no_nudge_below_threshold(self):
        """Nudge must NOT fire when new token delta is below threshold."""
        agent = _make_agent(nudge_threshold=0.8)
        ctx   = agent.context

        # Simulate last turn used 50% of the window (5 000 tokens)
        ctx.state["tokens_used"]              = 5_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        # Delta = 5 000; threshold = 0.8 * 10 000 = 8 000 → no nudge
        await _fire_nudge_hook(agent)

        nudge_turns = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert nudge_turns == []

    async def test_nudge_fires_at_threshold(self):
        """Nudge fires exactly when delta >= threshold * token_limit."""
        agent = _make_agent(nudge_threshold=0.8)
        ctx   = agent.context

        # Delta = 8 000 == threshold → should fire
        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent)

        nudge_turns = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert len(nudge_turns) == 1

    async def test_nudge_fires_above_threshold(self):
        """Nudge fires when delta exceeds the threshold."""
        agent = _make_agent(nudge_threshold=0.8)
        ctx   = agent.context

        ctx.state["tokens_used"]              = 9_500
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent)

        nudge_turns = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert len(nudge_turns) == 1

    async def test_nudge_does_not_repeat_before_new_delta(self):
        """
        After nudging, the baseline advances. A second hook run with the same
        tokens_used must NOT fire a second nudge (delta = 0).
        """
        agent = _make_agent(nudge_threshold=0.8)
        ctx   = agent.context

        ctx.state["tokens_used"]              = 9_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        # First run — nudge fires
        await _fire_nudge_hook(agent)
        assert len([e for e in ctx.dialogue if "Context is getting full" in e.content]) == 1

        # Second run — same token count, delta is now 0
        await _fire_nudge_hook(agent)
        nudge_turns = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert len(nudge_turns) == 1  # still only one

    async def test_nudge_recurs_after_sufficient_new_delta(self):
        """
        After the first nudge, once another threshold-worth of tokens
        accumulates, the nudge fires again.
        """
        agent = _make_agent(nudge_threshold=0.8)
        ctx   = agent.context

        # First nudge at 8 000
        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0
        await _fire_nudge_hook(agent)
        assert len([e for e in ctx.dialogue if "Context is getting full" in e.content]) == 1

        # Conversation continues; 8 000 more tokens accumulated since the nudge
        # baseline is now 8 000, so delta = 16 000 - 8 000 = 8 000 >= threshold
        ctx.state["tokens_used"] = 16_000
        await _fire_nudge_hook(agent)
        nudge_turns = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert len(nudge_turns) == 2

    async def test_nudge_message_contains_date(self):
        """The {date} placeholder is filled with today's date."""
        import datetime
        agent = _make_agent(
            nudge_threshold=0.0001,  # near-zero: fires on any new token
            nudge_message="Save to session-{date}.md now.",
        )
        ctx = agent.context
        ctx.state["tokens_used"]              = 1
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent)

        today = datetime.date.today().strftime("%d-%m-%Y")
        nudge_turns = [e for e in ctx.dialogue if today in e.content]
        assert len(nudge_turns) == 1

    async def test_nudge_disabled_when_threshold_zero(self):
        """
        nudge_threshold=0.0 is the opt-out sentinel: register() treats it as
        'disabled' and skips hook registration entirely. No nudge ever fires.
        Note: 0.0 does NOT mean 'always fire' — use a small positive value for that.
        """
        # threshold=0.0 → register() logs 'disabled' and skips hook registration
        agent = _make_agent(nudge_threshold=0.0)
        ctx   = agent.context

        ctx.state["tokens_used"]              = 9_999
        ctx.state["memory_nudge_tokens_at_last"] = 0

        # No hooks should be registered when disabled
        assert agent._background_hooks == []
        # No nudge turns injected
        nudge_turns = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert nudge_turns == []

    async def test_nudge_injects_user_turn(self):
        """The nudge is injected as a user-role HistoryEntry (legacy/no-DB path)."""
        agent = _make_agent(nudge_threshold=0.8)
        ctx   = agent.context

        ctx.state["tokens_used"]              = 8_500
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent)

        injected = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert len(injected) == 1
        assert injected[0].role == "user"


class TestContextNudgeDBBacked:
    """
    Phase 3: when a ConversationDB is wired, the nudge spawns a background
    branch instead of injecting inline. The live conversation is untouched.
    """

    async def test_nudge_does_not_inject_inline_when_db_wired(self, tmp_path):
        """Live context must NOT gain a nudge entry when a DB is present."""
        agent, db = _make_agent_with_db(tmp_path, nudge_threshold=0.8)
        ctx = agent.context

        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent, tail_node_id=ctx.tail_node_id)

        nudge_turns = [e for e in ctx.dialogue if "Context is getting full" in e.content]
        assert nudge_turns == []

    async def test_nudge_queues_background_branch(self, tmp_path):
        """queue_background_branch() must be called with a valid node_id."""
        agent, db = _make_agent_with_db(tmp_path, nudge_threshold=0.8)
        ctx = agent.context

        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent, tail_node_id=ctx.tail_node_id)

        assert len(agent._queued_background_branches) == 1
        branch_id = agent._queued_background_branches[0]
        assert db.get_node(branch_id) is not None

    async def test_branch_node_is_child_of_current_tail(self, tmp_path):
        """The opening branch node must be a direct child of the live tail."""
        agent, db = _make_agent_with_db(tmp_path, nudge_threshold=0.8)
        ctx = agent.context
        tail_before = ctx.tail_node_id

        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent, tail_node_id=tail_before)

        branch_id   = agent._queued_background_branches[0]
        branch_node = db.get_node(branch_id)
        assert branch_node.parent_id == tail_before

    async def test_branch_node_content_contains_nudge_message(self, tmp_path):
        """The opening node of the branch must carry the nudge message."""
        agent, db = _make_agent_with_db(
            tmp_path,
            nudge_threshold=0.8,
            nudge_message="Save to session-{date}.md.",
        )
        ctx = agent.context

        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent, tail_node_id=ctx.tail_node_id)

        branch_id   = agent._queued_background_branches[0]
        branch_node = db.get_node(branch_id)
        import datetime
        today = datetime.date.today().strftime("%d-%m-%Y")
        assert today in branch_node.content
        assert "Save to session-" in branch_node.content

    async def test_live_tail_does_not_advance_on_nudge(self, tmp_path):
        """The caller's cursor must not move — only the branch node is written."""
        agent, db = _make_agent_with_db(tmp_path, nudge_threshold=0.8)
        ctx = agent.context
        tail_before = ctx.tail_node_id

        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent, tail_node_id=tail_before)

        assert ctx.tail_node_id == tail_before

    async def test_nudge_does_not_repeat_before_new_delta_db(self, tmp_path):
        """Delta-based repeat-suppression works the same in DB-backed mode."""
        agent, db = _make_agent_with_db(tmp_path, nudge_threshold=0.8)
        ctx = agent.context

        ctx.state["tokens_used"]              = 8_000
        ctx.state["memory_nudge_tokens_at_last"] = 0

        await _fire_nudge_hook(agent, tail_node_id=ctx.tail_node_id)
        assert len(agent._queued_background_branches) == 1

        # Same token count — delta is now 0; no second branch
        await _fire_nudge_hook(agent, tail_node_id=ctx.tail_node_id)
        assert len(agent._queued_background_branches) == 1


# ---------------------------------------------------------------------------
# _format_results budget trimmer
# ---------------------------------------------------------------------------

# Import directly from the module file
import importlib, sys

# We need to import _format_results without triggering register()
# Load the module source and exec just the helper
_mem_main = importlib.import_module("modules.memory.__main__")
_format_results = _mem_main._format_results


class TestFormatResults:
    def _results(self, n: int, text_len: int = 100) -> list[dict]:
        return [
            {"file": f"file{i}.md", "text": "x" * text_len, "score": 1.0 - i * 0.1}
            for i in range(n)
        ]

    def test_empty_results_returns_none(self):
        assert _format_results([], budget_tokens=2048) is None

    def test_zero_budget_includes_all(self):
        results = self._results(5)
        out = _format_results(results, budget_tokens=0)
        assert out is not None
        assert out.count("[file") == 5

    def test_budget_limits_chunks(self):
        # Each chunk is ~100 chars = ~25 tokens; budget = 60 tokens → ~2 chunks
        results = self._results(10, text_len=100)
        out = _format_results(results, budget_tokens=60)
        assert out is not None
        # Should have fewer than 10 chunks
        chunk_count = out.count("[file")
        assert chunk_count < 10

    def test_truncation_note_appended(self):
        results = self._results(10, text_len=200)
        out = _format_results(results, budget_tokens=100)
        assert "omitted" in out

    def test_no_truncation_note_when_all_fit(self):
        results = self._results(2, text_len=10)
        out = _format_results(results, budget_tokens=10000)
        assert "omitted" not in out

    def test_output_wrapped_in_memory_tags(self):
        results = self._results(1)
        out = _format_results(results, budget_tokens=10000)
        assert out.startswith("<memory>")
        assert out.endswith("</memory>")

    def test_single_oversized_chunk_still_included(self):
        """Even if a single chunk exceeds the budget, include at least one."""
        results = [{"file": "big.md", "text": "y" * 10000, "score": 1.0}]
        out = _format_results(results, budget_tokens=10)
        # The only chunk should still be included (not return None)
        assert out is not None
        assert "big.md" in out

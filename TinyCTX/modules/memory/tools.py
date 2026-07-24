"""
modules/memory/tools.py

All graph-editing tools for the v2 memory system, plus shared helpers.

Scope handling
--------------
The visible/writable scope set is per-cycle and per-librarian-task, so it is
carried in a `contextvars.ContextVar` (`_scopes_var`) rather than a module
global — this is asyncio-task-local, so concurrent librarians with different
scopes never see each other's. Callers set it via `scope_context(...)` around a
tool invocation. Every read filters by it; every write validates its target
scope against it.

Tool exposure
-------------
Main agent:   search_memory, memory_stats, call_librarian (call_librarian lives
              in __main__.py).
Librarians:   all tools below.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
from pathlib import Path
from typing import Any

from TinyCTX.modules.memory import scopes as _scopes
from TinyCTX.modules.memory.graph import (
    embed_content_for,
    new_uuid,
    now_ts,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module singletons (set by init)
# ---------------------------------------------------------------------------

_conn: Any = None                 # ladybug AsyncConnection (write)
_write_lock: Any = None           # asyncio.Lock shared by ALL writers
_graph_db: Any = None             # GraphDB (sync reads)
_embedder: Any = None
_cfg: dict = {}
_data_dir: Path | None = None

_scopes_var: contextvars.ContextVar[set] = contextvars.ContextVar(
    "memory_scopes", default=frozenset({_scopes.GLOBAL})
)

# relation -> set of conflicting relations (mutually exclusive within a pair)
_CONFLICT_GROUPS: dict[str, set[str]] = {}
_DEFAULT_RELATIONS: list[str] = []


def init(conn, write_lock, graph_db, embedder, *, cfg: dict, data_dir: Path):
    global _conn, _write_lock, _graph_db, _embedder, _cfg, _data_dir
    _conn = conn
    _write_lock = write_lock
    _graph_db = graph_db
    _embedder = embedder
    _cfg = cfg or {}
    _data_dir = Path(data_dir)
    _load_relations()


# ---------------------------------------------------------------------------
# Scope context
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def scope_context(scope_set: set):
    """Bind the visible/writable scope set for the duration of a block."""
    token = _scopes_var.set(frozenset(scope_set))
    try:
        yield
    finally:
        _scopes_var.reset(token)


def current_scopes() -> set:
    return set(_scopes_var.get())


# ---------------------------------------------------------------------------
# Relation vocabulary + conflict groups
# ---------------------------------------------------------------------------

def _relations_file() -> Path:
    return Path(__file__).parent / "prompts" / "default_relations.txt"


def _load_relations() -> None:
    """Parse prompts/default_relations.txt. One group per line; members joined
    by '/' form a mutually-exclusive conflict group. Single tokens are
    standalone relations."""
    global _CONFLICT_GROUPS, _DEFAULT_RELATIONS
    _CONFLICT_GROUPS = {}
    _DEFAULT_RELATIONS = []
    try:
        lines = _relations_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        members = [m.strip().upper() for m in line.split("/") if m.strip()]
        for m in members:
            _DEFAULT_RELATIONS.append(m)
            if len(members) > 1:
                _CONFLICT_GROUPS[m] = set(members) - {m}


def default_relations() -> list[str]:
    return list(_DEFAULT_RELATIONS)


async def relation_vocab() -> str:
    """Default relations UNION live custom relations already in the graph, so
    librarian agents can reuse coined relations instead of re-inventing them."""
    defaults = default_relations()
    live: list[str] = []
    try:
        r = await _conn.execute(
            "MATCH ()-[r:Relation]->() RETURN DISTINCT r.relation ORDER BY r.relation"
        )
        while r and r.has_next():
            live.append(r.get_next()[0])
    except Exception:
        pass
    extras = [x for x in live if x and x not in defaults]
    return ", ".join(defaults + extras)


def _valid_relation(rel: str) -> bool:
    import re
    return bool(re.match(r"^[A-Z][A-Z0-9_]*$", rel))


# ---------------------------------------------------------------------------
# Shared write helpers (all under _write_lock at call sites)
# ---------------------------------------------------------------------------

async def _aset(uid: str, field: str, value):
    return await _conn.execute(
        f"MATCH (e:Entity) WHERE e.uuid = $uid SET e.{field} = $v",
        parameters={"uid": uid, "v": value},
    )


async def _touch(uid: str):
    await _aset(uid, "updated_at", now_ts())


async def _edge_exists(src_uid: str, tgt_uid: str, relation: str) -> bool:
    r = await _conn.execute(
        "MATCH (a:Entity {uuid:$s})-[r:Relation]->(b:Entity {uuid:$t}) "
        "WHERE r.relation = $rel RETURN 1 LIMIT 1",
        parameters={"s": src_uid, "t": tgt_uid, "rel": relation},
    )
    return bool(r and r.has_next())


def _mark_embed_stale(uid: str) -> None:
    """Drop the node from the live vector index; the embed pass re-adds it once
    re-embedded. Combined with embed_hash='' this is the dirty-set signal."""
    try:
        _graph_db.vector_index.remove(uid)
    except Exception:
        pass


def _resolve(name_or_uuid: str, visible: set) -> dict | None:
    """Slim dict by UUID (visible) or exact case-insensitive name in visible."""
    e = _graph_db.get_entity_slim(name_or_uuid, visible)
    if e:
        return e
    for m in _graph_db.find_by_name(name_or_uuid, visible):
        if m["name"].lower() == name_or_uuid.lower():
            return m
    return None


# ---------------------------------------------------------------------------
# Tools — read
# ---------------------------------------------------------------------------

async def search_memory(query: str, top_k: int = 5) -> str:
    """
    Search memory for entities relevant to a query, within the current scope.
    Exact name/UUID matches return immediately. Otherwise hybrid BM25 + vector
    search fused with RRF (min-p applied before fusion). Bumps mention by 1.

    Args:
        query: Natural-language query or exact entity name / UUID.
        top_k: Max entities to return (default 5).
    """
    from TinyCTX.utils.bm25 import BM25

    visible = current_scopes()
    min_p = float(_cfg.get("search_min_p", 0.0))
    rrf_k = int(_cfg.get("rrf_k", 60))
    bm25_w = float(_cfg.get("bm25_weight", 0.4))

    # -- exact match short-circuit --
    exact = _resolve(query, visible)
    if exact:
        _bump_mention([exact["uuid"]], 1.0)
        full = _graph_db.get_entity(exact["uuid"], visible)
        return _format_entities([full], exact_uuid=exact["uuid"]) if full else "No matching entities found."

    # -- BM25 --
    bm25_ranks: dict[str, int] = {}
    corpus = dict(_graph_db.bm25_corpus(visible))
    if corpus:
        hits = BM25(corpus).search(query, top_k=len(corpus))
        for rank, (uid, score) in enumerate((h for h in hits if h[1] > 0), start=1):
            bm25_ranks[uid] = rank

    # -- vector (min_p applied inside index.search, restricted to scope) --
    vec_ranks: dict[str, int] = {}
    if _embedder is not None and len(_graph_db.vector_index):
        try:
            qvec = await _embedder.embed_one(
                _cfg.get("embed_query_template", "{text}").format(text=query), priority=5
            )
            allowed = _graph_db.scoped_uuids(visible)
            hits = _graph_db.vector_index.search(qvec, k=len(allowed) or top_k, min_p=min_p, allowed=allowed)
            for rank, (uid, _score) in enumerate(hits, start=1):
                vec_ranks[uid] = rank
        except Exception as exc:
            logger.warning("[memory] search_memory vector failed: %s -- BM25 only", exc)

    fused = _rrf_fuse(bm25_ranks, vec_ranks, bm25_w=bm25_w, rrf_k=rrf_k)
    uids = [u for u, _ in fused[:top_k]]
    if not uids:
        return "No matching entities found."

    _bump_mention(uids, 1.0)
    ents = [_graph_db.get_entity(u, visible) for u in uids]
    return _format_entities([e for e in ents if e])


async def memory_stats() -> str:
    """
    Diagnostics for the current scope: entity counts by type, relationship count,
    pinned counts by scope, embedding coverage, and the reviewer backlog by
    issue type.
    """
    visible = current_scopes()
    s = _graph_db.get_stats(visible)
    lines = [
        f"Entities: {s['entity_count']}  |  Relationships: {s['edge_count']}  |  "
        f"Embedded: {s['embedded_count']}/{s['entity_count']}",
        "By type:",
    ]
    for et, n in sorted(s["by_type"].items(), key=lambda x: -x[1]):
        lines.append(f"  {et}: {n}")
    if s["pinned_by_scope"]:
        lines.append("Pinned by scope:")
        for sc, n in sorted(s["pinned_by_scope"].items()):
            lines.append(f"  {sc}: {n}")
    backlog = _reviewer_backlog_counts()
    if backlog:
        lines.append("Reviewer backlog:")
        for issue, n in sorted(backlog.items()):
            lines.append(f"  {issue}: {n}")
    else:
        lines.append("Reviewer backlog: empty")
    lines.append(_dedup_status_line())
    return "\n".join(lines)


def _dedup_status_line() -> str:
    """One-line dedup progress: suspected pairs and verification-call progress."""
    from TinyCTX.modules.memory.deduper import dedup_progress
    p = dedup_progress()
    pairs, done, total, merges = p["pairs"], p["groups_done"], p["groups_total"], p["merges"]
    if p["running"]:
        return (f"Dedup: running — {pairs} suspected duplicate pairs across {total} batches "
                f"→ {done}/{total} LLM calls done, {merges} merged")
    if p["finished_at"] is None:
        return "Dedup: idle (no run yet this session)"
    return (f"Dedup: idle — last run: {pairs} suspected pairs across {total} batches, "
            f"{done}/{total} LLM calls, {merges} merged")


# ---------------------------------------------------------------------------
# Tools — write
# ---------------------------------------------------------------------------

async def memory_add_entity(name: str, entity_type: str, description: str, scope: str = "global") -> str:
    """
    Add a new entity. Rejected if an entity with the same name already exists in
    the same scope — the existing entity's full data is returned so you can
    update or merge instead.

    Args:
        name: Display name.
        entity_type: e.g. Person, Project, Fact, Preference, Concept, Event.
        description: Freeform description.
        scope: Visibility scope (default "global"). Narrow (user:<name>,
            guild:<name>) ONLY for sensitive/personal info.
    """
    visible = current_scopes()
    if not _scopes.is_valid_scope(scope):
        return f"Error: invalid scope '{scope}'. Use 'global' or 'kind:target' (e.g. user:bob)."
    if scope not in visible:
        return f"Error: scope '{scope}' is not writable in this context. Writable: {sorted(visible)}."

    async with _write_lock:
        existing_uid = _graph_db.name_exists_in_scope(name, scope)
        if existing_uid:
            full = _graph_db.get_entity(existing_uid, visible)
            return "Error: '{}' already exists in scope '{}'.\n{}".format(
                name, scope, _format_entities([full]) if full else f"UUID: {existing_uid}"
            ) + "\nUse memory_update_entity_description or memory_merge_into instead."

        uid = new_uuid()
        now = now_ts()
        content = embed_content_for(name, entity_type, description)
        await _conn.execute("CREATE (e:Entity {uuid: $uid})", parameters={"uid": uid})
        await _aset(uid, "name", name)
        await _aset(uid, "entity_type", entity_type)
        await _aset(uid, "description", description)
        await _aset(uid, "scope", scope)
        await _aset(uid, "pinned", "")
        await _aset(uid, "mention", 0.0)
        await _aset(uid, "created_at", now)
        await _aset(uid, "updated_at", now)
        await _aset(uid, "embed_content", content)
        await _aset(uid, "embed_hash", "")  # dirty -> lazy embed
    return f"Added [{entity_type}] '{name}' (UUID: {uid}) scope={scope}"


async def memory_update_entity_description(name_or_uuid: str, description_diff: str) -> str:
    """
    Update an entity's description by applying a unified diff to it. Bumps
    mention by 1. Warns on a malformed diff or a stale base (concurrent edit).

    Args:
        name_or_uuid: Target entity.
        description_diff: Unified-diff (---/+++/@@ hunks) transforming the
            current description into the new one.
    """
    visible = current_scopes()
    async with _write_lock:
        target = _resolve(name_or_uuid, visible)
        if not target:
            return f"Entity '{name_or_uuid}' not found in scope."
        uid = target["uuid"]
        old = target.get("description", "") or ""
        ok, result = _apply_unified_diff(old, description_diff)
        if not ok:
            return f"Diff did not apply: {result}. Re-read the entity and regenerate the diff."
        new_desc = result
        content = embed_content_for(target["name"], target["entity_type"], new_desc)
        await _aset(uid, "description", new_desc)
        await _aset(uid, "embed_content", content)
        await _aset(uid, "embed_hash", "")
        await _aset(uid, "mention", _current_mention(uid) + 1.0)
        await _touch(uid)
        _mark_embed_stale(uid)
    return f"Updated description of '{target['name']}' (UUID: {uid})."


async def memory_set_entity_pinned(name_or_uuid: str, pinned: str) -> str:
    """
    Set/clear an entity's pin. A pinned entity is always injected into the
    memory block when its pin target is in the active scope. Use "" to unpin.

    Args:
        name_or_uuid: Target entity.
        pinned: Scope-grammar pin target ("global", "user:bob", ...) or "".
    """
    visible = current_scopes()
    if pinned != "" and not _scopes.is_valid_scope(pinned):
        return f"Error: invalid pin '{pinned}'. Use 'global', 'kind:target', or '' to unpin."
    async with _write_lock:
        target = _resolve(name_or_uuid, visible)
        if not target:
            return f"Entity '{name_or_uuid}' not found in scope."
        await _aset(target["uuid"], "pinned", pinned)
        await _touch(target["uuid"])
    return f"{'Pinned' if pinned else 'Unpinned'} '{target['name']}'" + (f" at {pinned}" if pinned else "")


async def memory_set_entity_scope(name_or_uuid: str, scope: str) -> str:
    """
    Change an entity's visibility scope.

    Args:
        name_or_uuid: Target entity.
        scope: New scope ("global" or "kind:target").
    """
    visible = current_scopes()
    if not _scopes.is_valid_scope(scope):
        return f"Error: invalid scope '{scope}'."
    async with _write_lock:
        target = _resolve(name_or_uuid, visible)
        if not target:
            return f"Entity '{name_or_uuid}' not found in scope."
        await _aset(target["uuid"], "scope", scope)
        await _touch(target["uuid"])
    return f"Set scope of '{target['name']}' to {scope}."


async def memory_delete_entity(name_or_uuid: str) -> str:
    """
    Hard-delete an entity and all its edges. Use sparingly.

    Args:
        name_or_uuid: Target entity.
    """
    visible = current_scopes()
    async with _write_lock:
        target = _resolve(name_or_uuid, visible)
        if not target:
            return f"Entity '{name_or_uuid}' not found in scope."
        uid = target["uuid"]
        await _conn.execute("MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e", parameters={"uid": uid})
        _mark_embed_stale(uid)
    return f"Deleted '{target['name']}' (UUID: {uid}) and all its edges."


async def memory_set_relationship(
    from_id: str, to_id: str, relationship_type: str, weight: float = 0.5
) -> str:
    """
    Create/update a directed relationship. SCREAMING_SNAKE_CASE only. Adding a
    relation in a conflict group (e.g. SUPERSEDES/DEPENDS_ON/CONFLICTS_WITH)
    deletes the conflicting relations between the same pair. If the same relation
    already exists between the pair, its weight is updated.

    Args:
        from_id: Source entity (name or UUID).
        to_id: Target entity (name or UUID).
        relationship_type: Relation label, SCREAMING_SNAKE_CASE.
        weight: Strength 0.0-1.0 (default 0.5).
    """
    visible = current_scopes()
    rel = relationship_type.strip().upper().replace(" ", "_")
    if not _valid_relation(rel):
        return f"Error: '{relationship_type}' is not valid SCREAMING_SNAKE_CASE."
    weight = max(0.0, min(1.0, float(weight)))
    async with _write_lock:
        src = _resolve(from_id, visible)
        tgt = _resolve(to_id, visible)
        if not src:
            return f"Source '{from_id}' not found in scope."
        if not tgt:
            return f"Target '{to_id}' not found in scope."
        s_uid, t_uid = src["uuid"], tgt["uuid"]

        # delete conflicting relations between this ordered pair
        conflicts = _CONFLICT_GROUPS.get(rel, set())
        for cr in conflicts:
            await _conn.execute(
                "MATCH (a:Entity {uuid:$s})-[r:Relation]->(b:Entity {uuid:$t}) "
                "WHERE r.relation = $cr DELETE r",
                parameters={"s": s_uid, "t": t_uid, "cr": cr},
            )

        # update weight if same relation exists, else create
        existing = await _conn.execute(
            "MATCH (a:Entity {uuid:$s})-[r:Relation]->(b:Entity {uuid:$t}) "
            "WHERE r.relation = $rel RETURN r.weight LIMIT 1",
            parameters={"s": s_uid, "t": t_uid, "rel": rel},
        )
        now = now_ts()
        if existing and existing.has_next():
            await _conn.execute(
                "MATCH (a:Entity {uuid:$s})-[r:Relation]->(b:Entity {uuid:$t}) "
                "WHERE r.relation = $rel SET r.weight = $w, r.updated_at = $now",
                parameters={"s": s_uid, "t": t_uid, "rel": rel, "w": weight, "now": now},
            )
            verb = "Updated"
        else:
            await _conn.execute(
                "MATCH (a:Entity {uuid:$s}), (b:Entity {uuid:$t}) "
                "CREATE (a)-[:Relation {relation:$rel, weight:$w, created_at:$now, updated_at:$now}]->(b)",
                parameters={"s": s_uid, "t": t_uid, "rel": rel, "w": weight, "now": now},
            )
            verb = "Added"
    conflict_note = f" (removed conflicting: {', '.join(sorted(conflicts))})" if conflicts else ""
    return f"{verb} '{src['name']}' -[{rel}]-> '{tgt['name']}' (w={weight}){conflict_note}"


async def memory_delete_relationship(from_id: str, to_id: str, relationship_type: str = "") -> str:
    """
    Delete a directed relation (from->to only; the reverse edge is untouched).
    Empty relationship_type deletes ALL relations between the ordered pair.

    Args:
        from_id: Source entity.
        to_id: Target entity.
        relationship_type: Relation label, or "" for all.
    """
    visible = current_scopes()
    async with _write_lock:
        src = _resolve(from_id, visible)
        tgt = _resolve(to_id, visible)
        if not src or not tgt:
            return "Source or target not found in scope."
        if relationship_type.strip():
            rel = relationship_type.strip().upper().replace(" ", "_")
            await _conn.execute(
                "MATCH (a:Entity {uuid:$s})-[r:Relation]->(b:Entity {uuid:$t}) "
                "WHERE r.relation = $rel DELETE r",
                parameters={"s": src["uuid"], "t": tgt["uuid"], "rel": rel},
            )
            what = f"[{rel}]"
        else:
            await _conn.execute(
                "MATCH (a:Entity {uuid:$s})-[r:Relation]->(b:Entity {uuid:$t}) DELETE r",
                parameters={"s": src["uuid"], "t": tgt["uuid"]},
            )
            what = "all relations"
    return f"Deleted {what} from '{src['name']}' to '{tgt['name']}'."


async def memory_merge_into(
    canonical: str, duplicate: str, merged_description: str, verdict: str = "duplicate"
) -> str:
    """
    Merge `duplicate` into `canonical`. verdict="duplicate" re-points the
    duplicate's edges to canonical (collapsing onto same-type relations), sets
    the merged description, and deletes the duplicate. verdict="alias" keeps both
    and adds duplicate -[ALIASED_TO]-> canonical.

    Args:
        canonical: Node to keep (name or UUID).
        duplicate: Node to absorb/alias (name or UUID).
        merged_description: Consolidated description for canonical.
        verdict: "duplicate" or "alias".
    """
    if verdict not in ("duplicate", "alias"):
        return "Error: verdict must be 'duplicate' or 'alias'."
    visible = current_scopes()
    async with _write_lock:
        c = _resolve(canonical, visible)
        d = _resolve(duplicate, visible)
        if not c:
            return f"Canonical '{canonical}' not found in scope."
        if not d:
            return f"Duplicate '{duplicate}' not found in scope."
        if c["uuid"] == d["uuid"]:
            return "Error: canonical and duplicate are the same entity."
        result = await _merge_internal(c, d, merged_description, verdict)
    return result


async def _merge_internal(c: dict, d: dict, merged_description: str, verdict: str) -> str:
    """Merge core (assumes _write_lock held). Shared by tool + deduper."""
    c_uid, d_uid = c["uuid"], d["uuid"]
    now = now_ts()
    if verdict == "alias":
        await _aset(c_uid, "description", merged_description)
        await _aset(c_uid, "embed_content", embed_content_for(c["name"], c["entity_type"], merged_description))
        await _aset(c_uid, "embed_hash", "")
        await _touch(c_uid)
        _mark_embed_stale(c_uid)
        await _aset(d_uid, "description", f"Aliased to {c['name']} (UUID {c_uid}).")
        await _touch(d_uid)
        await _conn.execute(
            "MATCH (a:Entity {uuid:$d}), (c:Entity {uuid:$c}) "
            "CREATE (a)-[:Relation {relation:'ALIASED_TO', weight:1.0, created_at:$now, updated_at:$now}]->(c)",
            parameters={"d": d_uid, "c": c_uid, "now": now},
        )
        return f"Aliased '{d['name']}' -> '{c['name']}'."

    # duplicate: re-point out-edges then in-edges, skipping self-edges and any
    # (relation, other-endpoint) pair the canonical already has (collapse onto
    # existing same-type relations). Done with explicit existence checks rather
    # than an EXISTS{} subquery for engine portability.
    out_edges = _graph_db._edges_from(d_uid, None)
    for e in out_edges:
        x = e["target_uuid"]
        if x == c_uid:
            continue
        if not await _edge_exists(c_uid, x, e["relation"]):
            await _conn.execute(
                "MATCH (c:Entity {uuid:$c}), (x:Entity {uuid:$x}) "
                "CREATE (c)-[:Relation {relation:$rel, weight:$w, created_at:$now, updated_at:$now}]->(x)",
                parameters={"c": c_uid, "x": x, "rel": e["relation"], "w": e.get("weight", 0.5), "now": now},
            )
    in_edges = _graph_db._edges_to(d_uid, None)
    for e in in_edges:
        x = e["source_uuid"]
        if x == c_uid:
            continue
        if not await _edge_exists(x, c_uid, e["relation"]):
            await _conn.execute(
                "MATCH (x:Entity {uuid:$x}), (c:Entity {uuid:$c}) "
                "CREATE (x)-[:Relation {relation:$rel, weight:$w, created_at:$now, updated_at:$now}]->(c)",
                parameters={"x": x, "c": c_uid, "rel": e["relation"], "w": e.get("weight", 0.5), "now": now},
            )
    await _aset(c_uid, "description", merged_description)
    await _aset(c_uid, "embed_content", embed_content_for(c["name"], c["entity_type"], merged_description))
    await _aset(c_uid, "embed_hash", "")
    await _aset(c_uid, "mention", _current_mention(c_uid) + _current_mention(d_uid))
    await _touch(c_uid)
    _mark_embed_stale(c_uid)
    await _conn.execute("MATCH (e:Entity) WHERE e.uuid = $uid DETACH DELETE e", parameters={"uid": d_uid})
    _mark_embed_stale(d_uid)
    return f"Merged '{d['name']}' into '{c['name']}' (edges reparented, duplicate deleted)."


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------

def _rrf_fuse(
    bm25_ranks: dict, vec_ranks: dict, *, bm25_w: float = 0.4, rrf_k: int = 60
) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion over two {uid: rank} maps. Inputs are assumed
    already min-p filtered. Returns [(uid, score)] descending."""
    vec_w = 1.0 - bm25_w
    scores: dict[str, float] = {}
    for uid, rank in bm25_ranks.items():
        scores[uid] = scores.get(uid, 0.0) + bm25_w / (rrf_k + rank)
    for uid, rank in vec_ranks.items():
        scores[uid] = scores.get(uid, 0.0) + vec_w / (rrf_k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _apply_unified_diff(base: str, diff: str) -> tuple[bool, str]:
    """
    Apply a unified diff to `base`. Returns (ok, new_text_or_error).
    Tolerant of a bare replacement: if the diff has no @@ hunks it's malformed.
    Uses a minimal hunk applier (no external deps).
    """
    lines = diff.splitlines()
    hunks = [ln for ln in lines if ln.startswith("@@")]
    if not hunks:
        return False, "malformed diff (no @@ hunk headers)"
    base_lines = base.splitlines()
    out: list[str] = []
    bi = 0
    i = 0
    # skip file headers
    while i < len(lines) and not lines[i].startswith("@@"):
        i += 1
    while i < len(lines):
        header = lines[i]
        if not header.startswith("@@"):
            i += 1
            continue
        import re
        m = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
        if not m:
            return False, f"malformed hunk header: {header}"
        start = int(m.group(1)) - 1
        if start < 0:
            start = 0
        # copy unchanged lines before the hunk
        while bi < start and bi < len(base_lines):
            out.append(base_lines[bi]); bi += 1
        i += 1
        while i < len(lines) and not lines[i].startswith("@@"):
            ln = lines[i]
            if ln.startswith(" "):
                if bi >= len(base_lines) or base_lines[bi] != ln[1:]:
                    return False, "context mismatch (stale base)"
                out.append(base_lines[bi]); bi += 1
            elif ln.startswith("-"):
                if bi >= len(base_lines) or base_lines[bi] != ln[1:]:
                    return False, "removed line mismatch (stale base)"
                bi += 1
            elif ln.startswith("+"):
                out.append(ln[1:])
            i += 1
    while bi < len(base_lines):
        out.append(base_lines[bi]); bi += 1
    return True, "\n".join(out)


def throttle_delay(queue_len: int, *, base: float, min_delay: float, target: int) -> float:
    """Inter-issue delay: long queue -> shorter waits (drain), short -> spaced."""
    if queue_len <= 0:
        return base
    d = base * (target / queue_len)
    return max(min_delay, min(base, d))


# ---------------------------------------------------------------------------
# Formatting + small DB reads
# ---------------------------------------------------------------------------

def _format_entities(entities: list[dict], exact_uuid: str | None = None) -> str:
    lines = []
    for e in entities:
        if not e:
            continue
        name = e.get("e.name", "?")
        et = e.get("e.entity_type", "?")
        uid = e.get("e.uuid", "?")
        desc = e.get("e.description", "")
        scope = e.get("e.scope", "")
        pin = e.get("e.pinned", "")
        tags = f" scope={scope}" + (f" pinned={pin}" if pin else "")
        exact = "  [exact]" if uid == exact_uuid else ""
        lines.append(f"[{et}] {name} (UUID: {uid}){tags}{exact}")
        if desc:
            lines.append(f"  {desc}")
        for edge in e.get("edges_out", []):
            lines.append(f"  ->[{edge['relation']}]-> {edge['target_name']} (UUID: {edge['target_uuid']}) (w={edge.get('weight')})")
        for edge in e.get("edges_in", []):
            lines.append(f"  <-[{edge['relation']}]<- {edge['source_name']} (UUID: {edge['source_uuid']}) (w={edge.get('weight')})")
        lines.append("")
    return "\n".join(lines).strip() or "No matching entities found."


def _current_mention(uid: str) -> float:
    try:
        r = _graph_db.safe_execute("MATCH (e:Entity {uuid:$u}) RETURN e.mention", {"u": uid})
        if r and r.has_next():
            v = r.get_next()[0]
            return float(v) if v is not None else 0.0
    except Exception:
        pass
    return 0.0


def _bump_mention(uids: list[str], amount: float) -> None:
    """Read-path bump via the sync connection (mention is agent-read-only).
    mention is always initialised to 0.0 on add, so no coalesce is needed."""
    for uid in uids:
        try:
            _graph_db.safe_execute(
                "MATCH (e:Entity {uuid:$u}) SET e.mention = e.mention + $a",
                {"u": uid, "a": amount},
            )
        except Exception:
            pass


def _reviewer_backlog_counts() -> dict[str, int]:
    if not _data_dir:
        return {}
    qf = _data_dir / "reviewer_queue.json"
    try:
        data = json.loads(qf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    counts: dict[str, int] = {}
    for issue in data.get("issues", []):
        t = issue.get("flagger_type", "unknown")
        counts[t] = counts.get(t, 0) + 1
    return counts

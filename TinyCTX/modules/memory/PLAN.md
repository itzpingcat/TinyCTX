# PLAN: Memory Graph Overhaul (v2)

**Feature:** Full rewrite of the TinyCTX long-term memory subsystem. Replaces the
single global `graph.lbug` + hard-delete decay + pinned-only injection design
with a scoped, passively-RAG'd graph in `memory.lbug`, a librarian architecture
split into Extractors / Reviewers / a Deduper, and flagger-driven maintenance
that never silently destroys data.

This document is the design of record. It resolves every open question surfaced
in review and specifies the concrete algorithms, schemas, and file layout the
implementation must follow. Where the old code already does the right thing, the
plan says "carry forward" rather than reinventing it.

---

## 0. Scope of the change

Basically everything in `modules/memory/` is rewritten. The old modules
(`graph.py`, `decay.py`, `dedup_agents.py`, `librarian_agents.py`, `tools.py`,
`__main__.py`) are replaced. What is deliberately **preserved** from the old
system because it already works:

- LadybugDB (Kùzu fork) as the store, opened via the existing WAL-safe
  `GraphDatabase` open/rebuild/checkpoint machinery.
- The SHA-256 content-hash embedding-staleness mechanism.
- The `sanitize_brackets` injection defense and the "never extract from the
  assistant's own turns" prompt rule.
- The `PromptProvider` + `post_turn_hook` integration surface in `register_agent`.
- RRF hybrid BM25 + vector fusion (with the min-p fix noted in §5).

What is **removed**: the `priority` field, the second (`graph_*`) embedding
model, the unconditional hard-delete decay sweep, and the `superseded_at`
soft-delete columns on relationships (tools already hard-delete edges; the
column is dead weight).

---

## 1. Storage & schema

Single file `memory.lbug`. Two node tables and one rel table.

```
NODE TABLE Entity (
    uuid           STRING PRIMARY KEY,
    name           STRING,
    entity_type    STRING,
    description    STRING,
    scope          STRING,          -- "global" | "user:<name>" | "guild:<id>" | ...
    pinned         STRING,          -- "" = unpinned; else scope-grammar target
    mention        DOUBLE,          -- agent-readable, agent cannot set directly
    created_at     DOUBLE,          -- agent-readable, not settable
    updated_at     DOUBLE,          -- agent-readable, not settable
    embed_hash     STRING,          -- SHA-256 of embed_content; "" = stale
    embed_content  STRING,          -- exact text last embedded (for audit)
    embedding      DOUBLE[]         -- variable-length; NULL when no model
)

NODE TABLE GraphMeta (key STRING PRIMARY KEY, val STRING)  -- schema version, migration flags

REL TABLE Relation (
    FROM Entity TO Entity,
    relation    STRING,             -- SCREAMING_SNAKE_CASE, validated at runtime
    weight      DOUBLE,             -- 0.0–1.0
    created_at  DOUBLE,
    updated_at  DOUBLE
)
```

Design decisions:

- **`priority` is gone.** It was written as a constant everywhere and only ever
  fed the decay formula. Its role in decay is replaced by the pin/edge/mention
  signals the decay flagger already has (see §7).
- **`mention` is a DOUBLE, not INT.** Passive RAG bumps it by a configurable
  fractional amount (default 0.1); `search_memory` bumps by 1.0.
- **`created_at`/`updated_at`/`mention` are agent-read-only.** They are returned
  in tool output but there is no tool parameter to set them. Tools set them as a
  side effect.
- **Schema version lives in `GraphMeta`.** A single `schema_version` key drives
  future forward migrations. We keep the idempotent-migration pattern from the
  old code but start clean at v2 (the old incremental `migration_*_v1` flags do
  not carry over; `migrate.py` in §11 handles the one-time old→new move).

`scope` and `pinned` both use the same **scope grammar** (§4) so one parser
serves both. This is intentional: a pin *is* a scope-shaped statement of "always
surface here."

---

## 2. Concurrency & consistency (was unaddressed)

LadybugDB is single-writer. Every write in the system funnels through **one
process-wide `asyncio.Lock` (`write_lock`)** held by the `GraphDatabase`
singleton and shared to every writer (main-agent tools, Extractors, Reviewers,
Deduper). Reads use short-lived read connections and do not take the lock.

- **Unique-name-in-scope is enforced atomically.** `memory_add_entity` performs
  the existence check and the `CREATE` *inside the same `write_lock` acquisition*.
  Two Extractors racing to add the same `(name, scope)` cannot both win: the
  second sees the first's node and is rejected. This closes the TOCTOU race the
  old code technically had (its check and create were both under the lock but
  the logic is now specified as a single critical section, not two calls).
- **Description-diff apply is lock-guarded and re-validated.** `memory_update_
  entity_description` reads the current description, applies the diff, and writes
  back all under one lock hold. If the base text the diff was generated against
  no longer matches (concurrent edit), the diff will not apply cleanly → return
  a distinct "stale base, re-read and retry" error (separate from "malformed
  diff"; see §6).
- **Reviewer queue durability.** The issue queue is **not** purely in-memory. It
  is persisted to `data/reviewer_queue.json` (in the agent-unreadable `data/`
  dir, §10) and rehydrated on startup, so `memory_stats`' backlog counts survive
  a restart and unprocessed issues are not lost. In-memory is the working copy;
  the file is written on enqueue/dequeue (debounced).

---

## 3. Vector index (concrete answers)

- **Hash input:** `embed_hash = sha256(embed_content)` where `embed_content` is
  the rendered string `"{name} ({entity_type})\n{description}"`. Name, type, and
  description all contribute; scope/pin/mention do **not** (they don't change
  semantics). Changing any hashed field zeroes `embed_hash`, marking the row for
  re-embed on the next embedding pass.
- **Model & dimension:** one configured embedding model (`embedding_model` in
  config). Dimension is model-dependent, so we keep the variable-length
  `DOUBLE[]` column and compute cosine in Python (numpy fast path, pure-Python
  fallback) — same rationale as the old code. Dropping the second model removes
  all `graph_*` columns.
- **Cache location & efficiency:** the vector index is an **in-memory matrix
  cache** (`{uuid: np.ndarray}` plus a stacked matrix for batched cosine) held by
  the `GraphDatabase` singleton, rebuilt lazily. Invalidation is driven by a
  cheap dirty-set: any write that zeroes an `embed_hash` (or deletes an entity)
  adds/removes that uuid from the cache. We never re-scan the whole table to
  detect staleness — the hash mismatch *is* the signal, and the writer that
  causes it registers the dirty uuid. A background embedding pass drains the
  dirty set, computes embeddings, writes `embedding` + `embed_hash`, and updates
  the matrix. Cache survives within a process; on cold start it is rebuilt from
  rows whose `embed_hash` is non-empty (no recompute needed for already-embedded
  rows).
- **Migration interaction:** `migrate.py` (§11) copies old `embedding` +
  `embed_hash` verbatim when the old and new embed_content rendering match;
  otherwise it zeroes `embed_hash` so the row re-embeds lazily. It never blocks
  migration on embedding calls.

---

## 4. Scoping (new — the largest genuinely-new subsystem)

Scope is an **information-isolation** mechanism, not an ownership tag. Format:
`scope_name:target`, or the bare literal `global`. Examples: `global`,
`user:itzpingcat`, `guild:1234`.

**Critical rule restated:** scope ≠ ownership. Most `Person` nodes are
`global`. Scope only restricts *where sensitive/personal info is visible*. A
node about user Able that is not sensitive should usually be `global` so it is
useful everywhere.

### 4.1 `scopes.py` — resolved once per AgentCycle

A dedicated `scopes.py` computes the **visible scope set** for the current
cycle from environment state:

```
resolve_scopes(env) -> set[str]
  # Always includes "global".
  # Adds "guild:<id>" if the conversation is in a guild.
  # Adds "user:<name>" for every human who spoke in the last N messages.
```

For the review example (Able + Bill in guild 1234): visible set =
`{global, guild:1234, user:able, user:bill}`. `user:carl` is **not** in the set,
so Carl's scoped nodes are invisible. This set is the single authority for
visibility.

### 4.2 Enforcement is at the tool/query layer, not just injection

This is the key regression fix: the old system filtered visibility only at
prompt-injection time; `kg_search`/`kg_traverse` saw the whole graph. In v2,
**every read path takes the visible-scope set and filters at the query.**
`search_memory`, passive RAG, `memory_stats`, and traversal all restrict to
`WHERE e.scope IN $visible`. There is no code path that returns a node outside
the caller's visible set.

### 4.3 Scope on writes (who decides global vs user:x)

- `memory_add_entity` takes an explicit `scope` param. **Default is `global`.**
- Extractors are the main writers. Their system prompt gives an explicit
  decision rule: *default to `global`; use `user:<name>` or `guild:<id>` ONLY
  for information that is personal, sensitive, private, or clearly meaningful
  only within that bucket.* This judgment is the Extractor's, but it is bounded:
  an Extractor may only write to scopes within the scope set it was handed by
  `scopes.py` (it cannot write `user:carl` if Carl isn't in the resolved set).
  A "too much personal data landed in `global`" case becomes a **flagger**
  (§9) rather than being relied on to never happen.

### 4.4 Scope on relationships

Edges are **not** independently scoped. An edge is visible iff **both endpoints
are visible** in the current scope set. So a `global` node A linked to
`user:able` node B: the edge is visible to Able (both endpoints visible) and
invisible to Bill (B not visible). This avoids a second scope grammar on edges
and makes visibility composable and predictable. Traversal never crosses into an
invisible node, so no edge can leak the existence of an out-of-scope entity.

---

## 5. Passive RAG & pinning

Implemented as a `PromptProvider` that emits a single `<memory>` block, refreshed
each turn via `post_turn_hook` (carrying forward the old cache/executor pattern).

### 5.1 Retrieval

Take the **last user message**, run hybrid retrieval over the visible-scope set:
BM25 + vector, fused with RRF.

- **min-p is applied *before* RRF** (explicit requirement). Each retriever drops
  candidates below its configured minimum similarity/score first; only survivors
  are ranked and fused. This prevents a globally-irrelevant node from riding a
  high reciprocal rank into the block just because the candidate pool was small.
- RAG can be **disabled** (config `passive_rag_enabled`), leaving only pinned
  entities in the block.

### 5.2 Pinning

`pinned` uses the scope grammar. Semantics: a pinned entity **bypasses the RAG
similarity search and is always present** in the `<memory>` block whenever its
pin target is in the current visible-scope set. `pinned == ""` means unpinned.
Option `pin_include_neighbors` (config): when on, also pull entities directly
linked to pinned entities into the block.

### 5.3 Deduplication of the block (specified, not just intended)

The block is assembled from up to three overlapping sources (BM25 hit, vector
hit, pinned, optionally pin-neighbors). Assembly:

1. Collect candidates from all sources into a `dict` keyed by **uuid** (set
   semantics — an entity present in three sources appears once). Record its
   provenance (pinned vs. rag) for ordering.
2. Order: **pinned first**, then RAG hits by fused score, then pin-neighbors.
3. Apply the token cap (`memory_block_tokens`) walking the ordered list.

### 5.4 Token-cap / over-budget behavior (was ambiguous)

- If **pinned entities alone exceed** the budget: pins are included in pin
  insertion order (a stable order — e.g. most-recently-updated first) until the
  budget is hit; the overflow is dropped and a single truncation marker line is
  emitted (`… N pinned entities omitted (token budget)`). Over-pinning at a
  scope is itself a flagger (§9), so this state is surfaced for cleanup rather
  than silently tolerated.
- RAG hits are only added after pins; if no room remains, none are added.

### 5.5 Mention accounting

- Passive retrieval of a **non-pinned** entity bumps `mention` by
  `passive_mention_bump` (default 0.1). Pinned entities are **not** bumped by
  passive injection (they're always there; bumping would be meaningless).
- `search_memory` bumps `mention` by 1.0.
- **Decay of mention:** because the destructive decay sweep is gone, `mention`
  would otherwise be a monotonic counter. To keep it meaningful for the
  "orphaned / junk" and "too little used" flaggers, a lightweight **recency
  half-life is applied at read time** when computing a node's effective
  mention-weight for flaggers (the stored value stays monotonic for audit; the
  flagger uses `mention * 0.5^(age_days / half_life)`). No data is deleted by
  this — it only affects flagger ranking.

---

## 6. Tools

All graph-editing tools live in **one `tools.py`** with shared helpers
(`_resolve(name_or_uuid)`, `_visible_scope_guard`, `_set_meta_timestamps`,
scope-grammar validate, relation validate).

**Main TinyCTX agent** gets only: `search_memory`, `memory_stats`,
`call_librarian`.
**Librarian subagents** get all tools below.

Relation validation: every relation string is uppercased and must match
`^[A-Z][A-Z0-9_]*$` (SCREAMING_SNAKE_CASE) or the call is rejected with a
corrective message. There is a default vocabulary (§8) that is *encouraged, not
enforced* — novel valid-format relations are allowed.

### `search_memory(query, top_k, min_p=cfg)`
Primary read path. **Exact match short-circuit:** if `query` exactly matches a
visible entity name or UUID, return that entity immediately (respecting
`min_p` only for the fuzzy path). Otherwise BM25 + vector with **min-p applied
before RRF**. Bumps `mention` by 1.0 on every returned node. Scope-filtered to
the caller's visible set.

### `memory_add_entity(name, entity_type, description, scope="global")`
Rejects if `(name)` already exists **in the same scope** (atomic, §2). Sets
`created_at`/`updated_at`. Zeroes `embed_hash` → registers dirty uuid.
- **On collision, return the existing entity's full data** (uuid, type, scope,
  pin, description, and its edges) in the rejection message — carry forward the
  old behavior — so the agent can decide whether to update/merge without a
  redundant `search_memory` round-trip.

### `memory_update_entity_description(name_or_uuid, description_diff)`
Applies a unified-diff to the existing description under the write lock. Bumps
`mention` by 1.0 on the target, sets `updated_at`, zeroes `embed_hash`.
- **Malformed diff** → warn, ask the agent to retry.
- **Clean-but-stale base** (concurrent edit moved the text) → distinct
  "description changed underneath you, re-read and regenerate the diff" error.

### `memory_set_entity_pinned(name_or_uuid, pinned)`
Validates `pinned` against scope grammar (or `""`). Sets `updated_at`.

### `memory_set_entity_scope(name_or_uuid, scope)`
Validates scope grammar. Sets `updated_at`.

### `memory_delete_entity(name_or_uuid)`
Hard-delete node + all edges (`DETACH DELETE`). Removes uuid from vector cache.

### `memory_set_relationship(from, to, relationship_type, weight)`
Adds an edge. **Conflict resolution:** relationship groups are declared with
`/` in the default list (§8) and are treated as **symmetric mutually-exclusive
sets** *between the same ordered pair*. Adding `SUPERSEDES` between A→B deletes
any existing `DEPENDS_ON` or `CONFLICTS_WITH` between A→B. If the *same*
relation already exists between the pair, **update its weight** instead of
duplicating. Scope of the edge is implicit (§4.4); both endpoints must be
visible to the caller.

### `memory_delete_relationship(from, to, relationship_type)`
Deletes the given relation type between the ordered pair. `relationship_type ==
""` deletes **all** relations between the pair. Directional: `from→to` only; the
reverse edge (`to→from`) is left untouched unless separately targeted.

### `memory_merge_into(canonical, duplicate, merged_description, verdict="duplicate")`
Librarian-only. Collapse `duplicate` into `canonical`:
- `verdict="duplicate"`: re-point all of the duplicate's in/out edges to
  `canonical` (skipping self-edges and collapsing onto existing relations of the
  same type via the weight-update rule), set `canonical.description =
  merged_description`, zero `canonical.embed_hash`, then `DETACH DELETE` the
  duplicate and drop its vector-cache entry.
- `verdict="alias"`: keep both, add `duplicate -[ALIASED_TO]-> canonical`
  (weight 1.0), rewrite the duplicate's description to a redirect stub, update
  the canonical description.
- Accepts UUID or exact name for both args; errors if either is unresolved, if
  they resolve to the same node, or if the two are not both visible in the
  caller's scope. Sets `updated_at` on the survivor(s). This is the explicit
  merge entry point (the Deduper pipeline in §10 calls the same internal helper).

### `memory_stats()`
Entity counts by type, relationship count, pinned counts by scope, embedding
coverage, top-mentioned — **all scope-filtered to the caller**. Also returns the
Reviewer backlog: count of pending issues **by flagger type** (read from the
persisted queue, §2).

### Removed old tools — disposition (no silent regression)
- `kg_traverse` → folded into `search_memory` result (each hit shows its direct
  edges) and available to librarians via a helper; a thin `memory_traverse` is
  retained **for librarians only** if Reviewers need multi-hop walks.
- `kg_get_entity` / `kg_list` → covered by `search_memory` exact-match + stats.
- `kg_merge_entities` → **replaced** by `memory_merge_into` (librarian-only,
  above) and the Deduper pipeline (§10), which share one internal merge helper.
  Not exposed to the main agent.
- **No rename / retype tool** exists in the old system either; v2 keeps it that
  way. Renaming = add new + merge, retyping likewise. If direct rename/retype is
  wanted, it's a small addition — flagged, not assumed.

---

## 7. Decay becomes a flagger (not a deleter)

The old sweep min-max-normalized five factors **relative to the current
candidate set every run** and hard-`DETACH DELETE`d anything below threshold —
which is exactly how legitimately-important-but-quiet nodes got destroyed. v2
kills the auto-deleter entirely.

Decay is reborn as a **Reviewer flagger** (§9) that identifies *candidates for
review* — low effective-mention (§5.5), few edges, far from any pinned node,
stale `updated_at` — and enqueues them as "stale, assess" issues. The Reviewer
LLM then decides: link it up, leave it, or delete it. **Nothing is deleted
without a judgment step.** Thresholds are absolute and configurable, not
relative to the sweep's population, so a quiet-but-important node is never
mechanically doomed.

---

## 8. Default relationship vocabulary

Encouraged, not enforced. Groups joined by `/` are mutually-exclusive between an
ordered pair (adding one deletes the others in the group):

```
LIKES/DISLIKES
PRECEDED_BY/FOLLOWED_BY
IS_A/IS_NOT
INSTANCE_OF/PART_OF
PERMITS/PROHIBITS
SUPERSEDES/DEPENDS_ON/CONFLICTS_WITH
KNOWS
SKILLED_IN
CREATED
USES
OWNS
WORKS_AT
MEMBER_OF
CAUSED
ENFORCES
LOCATED_IN
RELATED_TO
WANTS_TO_KNOW
WANTS_TO_TEACH
```

Stored in `prompts/default_relations.txt`, parsed into (a) the vocab string
injected into librarian prompts and (b) a `{relation: group_set}` map driving
conflict deletion in `memory_set_relationship`. `IS_NOT` doubles as the marker
the fuzzy-dedup flagger writes to record "these two are distinct" (§9).

**Vocab injected into librarian prompts = defaults ∪ live custom relations.**
`get_relation_types(conn)` returns the default list *plus* every distinct
relation label already present in the graph that isn't a default (carry forward
the old union behavior), so librarian agents see and can reuse relations coined
in earlier cycles rather than re-inventing near-duplicates. Only the default
groups carry `/` conflict semantics; custom relations are standalone.

---

## 9. Librarian subagents

Background agents that maintain the graph. All share the full toolset. Each type
has a script that resolves **exactly** the scope it needs and nothing more.

### 9.1 Extractors
Pull unvisited branches from `agent.db` (carry forward the `flag_branch(...,
"librarian_visited")` mechanism) and ingest their information. Scope resolution =
environment state + the humans who spoke in the last N messages (via
`scopes.py`). Context is built with `sanitize_brackets` (carry forward the
existing injection defense) and the "never extract from the assistant's own
turns" rule. Extractors default new nodes to `global` and only narrow scope for
sensitive info (§4.3).

### 9.2 Reviewers + Flaggers
**Flaggers** are short graph-scanning snippets in `/flaggers`, dynamically
loaded at runtime. Each flagger:
- scans the graph (or a scope) and yields issues,
- builds its own prompt fragment appended to a shared Reviewer base prompt,
- declares the scope the Reviewer should operate in.

The **Reviewer orchestrator** loads all flaggers, runs them, and appends new
issues to the persisted queue (§2). Concrete mechanics:

- **Issue identity / dedup key:** `(flagger_type, sorted(entity_uuids))`. An
  issue already in the queue (by this key) is never appended twice.
- **Dedup vs. processing interlock:** the queue is **not** processed while a
  dedup/append pass is running (a simple `asyncio.Event` gate). This prevents
  processing a half-deduplicated queue.
- **Throttle (concrete):** inter-issue delay scales with backlog so we're not
  bursty. `delay = clamp(base_delay * (target_len / max(len,1)), min_delay,
  base_delay)` — i.e. long queue → shorter waits (drain faster), short queue →
  spaced out. All three constants are config (`reviewer_base_delay`,
  `reviewer_min_delay`, `reviewer_target_len`). No magic numbers.

Example flaggers (each self-contained in `/flaggers`):
- **Too many edges between the same pair** (even if different relations).
- **Description too long** → split into specialized entities.
- **Description too short** → assess whether it's junk.
- **Orphaned entity** → link up or destroy.
- **Too many pins at scope X** → propose unpins.
- **Decay candidate** (§7) → stale/quiet/isolated node, assess.
- **Near-duplicate names** via `rapidfuzz` (§9.4).

### 9.3 `call_librarian` (main agent)
Enqueues a flagged issue at the **front** of the Reviewer queue (priority jump).
Same dedup key applies.

### 9.4 Fuzzy-name flagger vs. embedding Deduper (relationship clarified)
These are **complementary, independent** duplicate detectors:
- **Fuzzy-name flagger** (`rapidfuzz`, threshold `fuzzy_name_threshold`, default
  ~90) catches lexical near-duplicates that *embeddings miss or mis-score*. It
  **ignores pairs already linked by `IS_NOT`**. If the Reviewer decides two
  fuzzy-matched nodes are genuinely distinct, it writes an `IS_NOT` edge so the
  pair is never re-flagged. It runs on a schedule over the graph (not every
  cycle — `fuzzy_scan_interval_hours`) and **may compare across different
  scopes**, leaving the merge/keep decision (and any scope reconciliation) to the
  Reviewer.
- **Embedding Deduper** (§10) catches *semantic* duplicates with different
  surface names. Different signal, different pipeline. Neither supersedes the
  other; overlap (a pair caught by both) is harmless because the queue dedup key
  and the dedup cache both prevent double-work.

---

## 10. Deduplication pipeline

Runs on a schedule. Steps:
1. **Embedding pass** generates candidate pairs (cosine ≥ `similarity_threshold`)
   over the up-to-date vector cache.
2. **Greedy Clique-Edge-Cover** groups candidates into batches
   (`dedup_batch_count`) so each LLM call verifies a coherent cluster (carry
   forward the old `_clique_edge_cover`).
3. LLM verifies duplicates per batch; confirmed dups are merged.
4. **Dedup cache** records already-compared pairs so we don't re-spend on them.
   Location: a sidecar under `data/` (carry forward the old separate-SQLite
   pattern — `data/dedup_cache.db`, table `distinct_pairs`) so it stays in the
   agent-unreadable dir and doesn't bloat `memory.lbug`.

Merge itself re-points edges from duplicate to canonical, consolidates the
description, deletes the duplicate, and invalidates both vector-cache entries.

---

## 11. Migration (`migrate.py`)

One-shot, guarded, **not silently destructive**.

```
if graph.lbug exists and memory.lbug does not:
    open old graph.lbug (read)
    create memory.lbug (new v2 schema)
    stream entities:
        name, entity_type, description  -> copied 1:1
        pinned_target ("global"|user)   -> pinned  (grammar: "global" | "user:<name>")
        pinned_target                   -> also seeds scope? NO — scope defaults "global"
        priority                        -> DROPPED (logged)
        mention_count                   -> mention (as DOUBLE)
        created_at/updated_at           -> copied
        embedding/embed_hash            -> copied if embed_content rendering matches,
                                           else embed_hash="" (lazy re-embed)
        graph_* columns                 -> DROPPED
    stream relationships:
        relation, weight                -> copied
        superseded_at IS NOT NULL       -> SKIPPED (soft-deleted edges are dead)
        created_at                      -> copied; updated_at = created_at
    write GraphMeta schema_version = "2"
    verify counts (entities in ≈ entities out, minus intentional drops)
    only on success: rename graph.lbug -> graph.lbug.migrated.bak (NOT deleted outright)
```

Safety additions over the bare spec:
- **Dry-run mode** (`--dry-run`) reports what would move without writing.
- **The old file is renamed to `.bak`, not `rm`'d**, on first success. A
  follow-up `--purge` deletes the backup once the user confirms the new graph is
  good. A one-way delete mid-migration is the one place a crash could lose
  everything, so we never delete before the new DB is verified and reopened.
- **Old→new mapping is 1:1 for weight, mention, and pin equivalents**, which is
  the assumption to confirm with Kamie. `scope` is *not* inferred from
  `pinned_target` (pin target ≠ visibility scope in v2) — everything migrates to
  `global` scope, and narrowing is left to Reviewers/Extractors afterward. This
  is the safe default: nothing that was globally visible becomes invisible.

---

## 12. Security / data-leak surface

- **Librarian log & queue & dedup cache live under `data/`**, which the agent's
  file tools cannot read (carry forward). The Reviewer queue file and dedup cache
  join the log there.
- **Extractor context is the highest-risk injection surface**, not the log.
  `sanitize_brackets` (the fullwidth-bracket 【】 spoofing guard) is applied to
  all conversation text before it enters an Extractor prompt, and the
  "never extract facts from the assistant's own turns" rule is retained. This is
  the same mechanism the spec calls "the unicode sanitization trick," reused, not
  reinvented.
- **Scope enforcement is a confidentiality boundary**, so it lives at the query
  layer (§4.2) where it can't be bypassed by an agent choosing a different read
  tool.

---

## 13. Config (single source of truth, no magic values)

All thresholds in `config.yaml` under `memory:`. Defaults consolidated here to
end the old drift between `__init__.py` defaults and hardcoded fallbacks (the old
code disagreed on dedup interval, similarity threshold, and block tokens across
files — v2 reads config in exactly one place and has no fallback constants).

```
graph_path:               memory/memory.lbug
data_dir:                 data/                 # log, reviewer_queue.json, dedup_cache.db

embedding_model:          ""                    # "" = BM25-only
embed_query_template:     "{text}"
embed_document_template:  "{text}"

passive_rag_enabled:      true
memory_block_tokens:      2048
passive_min_p:            0.30                  # applied BEFORE RRF
bm25_weight:              0.40
passive_mention_bump:     0.1
pin_include_neighbors:    false
mention_half_life_days:   30                    # read-time weighting for flaggers only

reviewer_base_delay:      30                    # seconds between issues, short queue
reviewer_min_delay:       2
reviewer_target_len:      10
fuzzy_name_threshold:     90
fuzzy_scan_interval_hours: 12

dedup_enabled:            true
dedup_interval_hours:     6
similarity_threshold:     0.90
dedup_batch_count:        8

extractor_batch_size:     20
extractor_max_concurrent: 4
ingest_pressure_ratio:    0.5
ingest_pressure_min_tokens: 500
```

---

## 14. File layout (flat, ≤2 levels, one job per module)

```
modules/memory/
  __init__.py            EXTENSION_META + consolidated default_config
  __main__.py            register_runtime / register_agent, LibrarianRunner, providers
  graph.py               GraphDatabase (open/rebuild/checkpoint), schema, vector cache,
                         embed helpers, cosine  (carry-forward WAL machinery)
  scopes.py              resolve_scopes(env) -> set[str]   (NEW)
  tools.py               all graph-editing tools + shared helpers
  extractor.py           Extractor agent loop + nodes_to_text (sanitized)
  reviewer.py            Reviewer orchestrator: load flaggers, queue, throttle
  deduper.py             embedding candidates + clique-cover + verify + cache
  migrate.py             one-shot old->new migration (dry-run / bak / purge)
  flaggers/              dynamically-loaded flagger snippets
    too_many_edges.py
    description_length.py
    orphaned.py
    over_pinned.py
    decay_candidate.py
    fuzzy_names.py
  prompts/
    extractor_system.txt / extractor_user.txt
    reviewer_system.txt
    dedup_system.txt / dedup_group_user.txt
    default_relations.txt
```

Every module has a one-sentence job; all files target < 600 lines (split
`tools.py` helpers into the file's own section, not a new module, unless it
exceeds the limit).

---

## 15. Verification plan (success criteria)

Tests live in `/tests` (the old `test_memory.py` was deleted — start fresh).

1. **Scope isolation** — Able+Bill+guild fixture: assert `search_memory` and the
   passive block never return a `user:carl` node; assert an edge between a
   `global` and a `user:able` node is invisible to Bill. → *the core
   confidentiality guarantee.*
2. **Atomic unique-name** — two concurrent `memory_add_entity` on the same
   `(name, scope)`: exactly one succeeds.
3. **Relation conflict deletion** — add `DEPENDS_ON` then `SUPERSEDES` A→B:
   assert `DEPENDS_ON` gone, reverse edge untouched.
4. **Block dedup + token cap** — entity hit by BM25+vector+pin appears once;
   over-budget pins emit the truncation marker.
5. **min-p before RRF** — a low-similarity node in a tiny pool is excluded.
6. **Decay never auto-deletes** — decay flagger enqueues; assert no node deleted
   without a Reviewer step.
7. **Vector cache invalidation** — description edit zeroes `embed_hash`, dirty
   set contains the uuid, re-embed updates the matrix.
8. **Migration fidelity** — old fixture graph → assert entity/edge counts,
   weight/mention/pin carried, `priority`/`graph_*`/superseded edges dropped,
   old file renamed to `.bak` not deleted, `--dry-run` writes nothing.
9. **Queue durability** — enqueue issues, restart, assert `memory_stats` backlog
   counts survive.

High-stakes items (scope isolation, migration) additionally get a
subagent-driven review pass before merge.

---

## 16. Resolved decisions

All open questions are settled — no assumptions remain.

- **Rename / retype: add-then-merge.** No `memory_rename` / `memory_set_type`
  tool. Renaming = `memory_add_entity` the new node then `memory_merge_into` the
  old one into it; retyping is the same. (§6)
- **Merge:** explicit librarian-only `memory_merge_into` (duplicate/alias),
  sharing one internal helper with the Deduper. (§6, §10)
- **Add-entity collision:** returns the existing node's full data (uuid, type,
  scope, pin, description, edges) in the rejection message. (§6)
- **Librarian relation vocab:** defaults ∪ live custom relations; only default
  `/` groups carry conflict semantics. (§8)
- **Migration scope:** every old node migrates to `global` scope — nothing that
  was globally visible becomes invisible; narrowing is left to Reviewers /
  Extractors afterward. `pinned_target=user:x` does **not** seed `scope:user:x`.
  (§11)
- **`mention` half-life:** stored `mention` stays monotonic (audit); the recency
  half-life is applied only at read time for flagger ranking. No stored decayed
  value, nothing deleted by it. (§5.5)

"""
db.py — SQLite-backed conversation tree.

Every message is a node. Nodes form a tree via parent_id. There is one
global root node (role=system, content="") created at DB initialisation.

This module has zero imports from the rest of TinyCTX — no contracts,
no config, no agent. It owns the SQLite connection and all node I/O.

Public API
----------
ConversationDB(path)          — open (or create) the database
  .ensure_schema()            — idempotent; creates tables + root node
  .add_node(parent_id, ...)   — insert a node; returns Node
  .get_node(node_id)          — fetch one node or None
  .get_parent(node_id)        — fetch parent node or None (one hop up)
  .get_ancestors(node_id)     — [root, ..., node] order
  .get_children(node_id)      — direct children (unordered)
  .get_root()                 — the single global root node
  .get_tail_nodes()           — all nodes with no children
  .load_session_state(node_id, threshold) — walk delta chain, return (state, depth)
  .write_checkpoint_if_needed(node_id, state, depth, threshold) — write checkpoint
  .add_flag(node_id, flag)    — add a flag string to a node's flags list
  .remove_flag(node_id, flag) — remove a flag string from a node's flags list
  .has_flag(node_id, flag)    — check if a node has a flag
  .get_flags(node_id)         — return the flags list for a node
  .get_nodes_without_flag(flag) — all nodes missing the given flag
  .flag_branch(node_id, flag) — walk parent chain, flag nodes until one already has it
  .close()                    — close the connection

Phase 2 additions
-----------------
  author_name      TEXT — display name of the message sender (group chats)
  attachment_paths TEXT — JSON list of workspace-relative upload paths
  state_delta      TEXT — JSON object of changed session-state keys; may
                          include "_checkpoint": true for full snapshots

Phase 3 additions
-----------------
  flags            TEXT — JSON array of flag strings, e.g. '["librarian_visited"]'
                          Generic module-use column; not specific to any one module.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_VALID_ROLES = {"user", "assistant", "system", "tool"}


# ---------------------------------------------------------------------------
# Node dataclass
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id:               str
    parent_id:        str | None
    role:             str
    content:          str
    created_at:       float
    tool_calls:       str | None
    tool_call_id:     str | None
    author_id:        str | None
    author_name:      str | None
    attachment_paths: str | None
    state_delta:      str | None
    flags:            str = "[]"     # JSON array of flag strings


# ---------------------------------------------------------------------------
# ConversationDB
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id               TEXT PRIMARY KEY,
    parent_id        TEXT,
    role             TEXT NOT NULL,
    content          TEXT NOT NULL,
    created_at       REAL NOT NULL,
    tool_calls       TEXT,
    tool_call_id     TEXT,
    author_id        TEXT,
    author_name      TEXT,
    attachment_paths TEXT,
    state_delta      TEXT,
    flags            TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (parent_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the initial schema — applied to existing DBs via ALTER TABLE.
# Note: ALTER TABLE in SQLite does not support NOT NULL without a default, and
# older SQLite versions reject NOT NULL in ADD COLUMN entirely. Use plain DEFAULT.
_MIGRATIONS = [
    "ALTER TABLE nodes ADD COLUMN author_name      TEXT",
    "ALTER TABLE nodes ADD COLUMN attachment_paths TEXT",
    "ALTER TABLE nodes ADD COLUMN state_delta      TEXT",
    "ALTER TABLE nodes ADD COLUMN flags            TEXT DEFAULT '[]'",
]

_COLS = "id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id, author_name, attachment_paths, state_delta, flags"

_INSERT_NODE = f"""
INSERT INTO nodes ({_COLS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_NODE = f"SELECT {_COLS} FROM nodes WHERE id = ?"

_SELECT_PARENT = """
SELECT {cols} FROM nodes WHERE id = (
    SELECT parent_id FROM nodes WHERE id = ?
)
""".format(cols=_COLS)

_ANCESTORS_CTE = f"""
WITH RECURSIVE anc AS (
    SELECT {_COLS} FROM nodes WHERE id = ?
    UNION ALL
    SELECT {', '.join('n.' + c for c in _COLS.split(', '))}
    FROM nodes n JOIN anc a ON n.id = a.parent_id
)
SELECT {_COLS} FROM anc
"""

_CHILDREN = f"SELECT {_COLS} FROM nodes WHERE parent_id = ? ORDER BY created_at"

_TAIL_NODES = f"""
SELECT {_COLS} FROM nodes
WHERE id NOT IN (SELECT DISTINCT parent_id FROM nodes WHERE parent_id IS NOT NULL)
"""


def _row_to_node(row: tuple) -> Node:
    return Node(
        id=row[0],
        parent_id=row[1],
        role=row[2],
        content=row[3],
        created_at=row[4],
        tool_calls=row[5],
        tool_call_id=row[6],
        author_id=row[7],
        author_name=row[8],
        attachment_paths=row[9],
        state_delta=row[10],
        flags=row[11] if row[11] is not None else "[]",
    )


def _parse_flags(flags_json: str) -> list[str]:
    try:
        result = json.loads(flags_json or "[]")
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


class ConversationDB:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create tables, apply migrations, and insert the global root node if needed."""
        with self._conn:
            self._conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'root_id'").fetchone()
        if row is None:
            root_id = str(uuid.uuid4())
            self._conn.execute(
                _INSERT_NODE,
                (root_id, None, "system", "", time.time(), None, None, None, None, None, None, "[]"),
            )
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('root_id', ?)", (root_id,)
            )
            self._conn.commit()

    def get_root(self) -> Node:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'root_id'").fetchone()
        if row is None:
            raise RuntimeError("ConversationDB: root node missing (schema not applied?)")
        node = self.get_node(row[0])
        if node is None:
            raise RuntimeError("ConversationDB: root_id in meta but node row missing")
        return node

    def add_node(
        self,
        parent_id: str,
        role: str,
        content: str,
        *,
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        author_id: str | None = None,
        author_name: str | None = None,
        attachment_paths: str | None = None,
        state_delta: str | None = None,
    ) -> Node:
        if role not in _VALID_ROLES:
            raise ValueError(
                f"ConversationDB.add_node: invalid role {role!r}. "
                f"Must be one of {sorted(_VALID_ROLES)}."
            )
        if not parent_id or not isinstance(parent_id, str):
            raise ValueError(
                "ConversationDB.add_node: parent_id must be a non-empty string. "
                "Only the global root node may have parent_id=None."
            )
        node_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            _INSERT_NODE,
            (node_id, parent_id, role, content, now, tool_calls, tool_call_id,
             author_id, author_name, attachment_paths, state_delta, "[]"),
        )
        self._conn.commit()
        return Node(
            id=node_id,
            parent_id=parent_id,
            role=role,
            content=content,
            created_at=now,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            author_id=author_id,
            author_name=author_name,
            attachment_paths=attachment_paths,
            state_delta=state_delta,
            flags="[]",
        )

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute(_SELECT_NODE, (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def get_parent(self, node_id: str) -> Node | None:
        """Return the immediate parent of node_id, or None if node_id is root."""
        row = self._conn.execute(_SELECT_PARENT, (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def get_ancestors(self, node_id: str) -> list[Node]:
        """
        Return the ancestor chain in root → node order (inclusive of node_id).
        The root node itself is excluded — it's a structural placeholder with
        no dialogue content.
        """
        rows = self._conn.execute(_ANCESTORS_CTE, (node_id,)).fetchall()
        nodes = [_row_to_node(r) for r in rows]
        nodes.reverse()
        if nodes and nodes[0].parent_id is None and nodes[0].content == "":
            nodes = nodes[1:]
        nodes = [n for n in nodes if not (n.role == "system" and n.content.startswith("session:"))]
        return nodes

    def get_children(self, node_id: str) -> list[Node]:
        rows = self._conn.execute(_CHILDREN, (node_id,)).fetchall()
        return [_row_to_node(r) for r in rows]

    def get_tail_nodes(self) -> list[Node]:
        """Return all nodes that have no children (leaf nodes)."""
        rows = self._conn.execute(_TAIL_NODES).fetchall()
        return [_row_to_node(r) for r in rows]

    def update_node_content(self, node_id: str, content: str) -> bool:
        """Update a node's content in-place. Returns True if found."""
        cur = self._conn.execute(
            "UPDATE nodes SET content = ? WHERE id = ?", (content, node_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_node_state_delta(self, node_id: str, state_delta: str) -> bool:
        """Update a node's state_delta in-place. Returns True if found."""
        cur = self._conn.execute(
            "UPDATE nodes SET state_delta = ? WHERE id = ?", (state_delta, node_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_node(self, node_id: str) -> bool:
        """Delete a single node row. Does NOT cascade — callers handle dependents."""
        cur = self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Flag utilities
    # ------------------------------------------------------------------

    def get_flags(self, node_id: str) -> list[str]:
        """Return the flags list for a node. Returns [] if node not found."""
        row = self._conn.execute("SELECT flags FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return []
        return _parse_flags(row[0])

    def has_flag(self, node_id: str, flag: str) -> bool:
        """Return True if the node has the given flag."""
        return flag in self.get_flags(node_id)

    def add_flag(self, node_id: str, flag: str) -> None:
        """Add a flag to a node's flags list. No-op if already present."""
        flags = self.get_flags(node_id)
        if flag in flags:
            return
        flags.append(flag)
        self._conn.execute(
            "UPDATE nodes SET flags = ? WHERE id = ?",
            (json.dumps(flags), node_id),
        )
        self._conn.commit()

    def remove_flag(self, node_id: str, flag: str) -> None:
        """Remove a flag from a node's flags list. No-op if not present."""
        flags = self.get_flags(node_id)
        if flag not in flags:
            return
        flags = [f for f in flags if f != flag]
        self._conn.execute(
            "UPDATE nodes SET flags = ? WHERE id = ?",
            (json.dumps(flags), node_id),
        )
        self._conn.commit()

    def get_nodes_without_flag(self, flag: str) -> list[Node]:
        """Return all nodes that do not have the given flag."""
        rows = self._conn.execute(
            "SELECT " + _COLS + " FROM nodes WHERE flags NOT LIKE ?",
            (f'%"{flag}"%',),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def flag_branch(self, node_id: str, flag: str) -> list[str]:
        """
        Walk the parent chain from node_id upward, adding flag to each node,
        stopping at (and excluding) the first ancestor that already has the flag.

        Returns the list of node_ids that were flagged (tip → root order).
        Returns [] if node_id itself already has the flag.
        """
        flagged: list[str] = []
        current_id: str | None = node_id

        while current_id is not None:
            node = self.get_node(current_id)
            if node is None:
                break
            existing = _parse_flags(node.flags)
            if flag in existing:
                break
            existing.append(flag)
            self._conn.execute(
                "UPDATE nodes SET flags = ? WHERE id = ?",
                (json.dumps(existing), current_id),
            )
            flagged.append(current_id)
            current_id = node.parent_id

        if flagged:
            self._conn.commit()

        return flagged

    # ------------------------------------------------------------------
    # Session state helpers
    # ------------------------------------------------------------------

    def load_session_state(self, node_id: str) -> tuple[dict, int]:
        """
        Walk ancestors tip→root one hop at a time, merging state_delta JSON
        objects to reconstruct current session state.

        Stops early when it hits a node with "_checkpoint": true.

        Returns (state_dict, depth) where depth is the number of nodes visited.
        Keys filled by earlier (tip-side) nodes win (most-recent wins).
        """
        state: dict = {}
        depth = 0
        current_id: str | None = node_id

        while current_id is not None:
            node = self.get_node(current_id)
            if node is None:
                break
            depth += 1

            if node.state_delta:
                try:
                    delta: dict = json.loads(node.state_delta)
                except (json.JSONDecodeError, ValueError):
                    delta = {}
                for k, v in delta.items():
                    if k not in state:
                        state[k] = v
                if delta.get("_checkpoint"):
                    break

            if node.parent_id is None:
                break
            current_id = node.parent_id

        state.pop("_checkpoint", None)
        return state, depth

    def write_checkpoint_if_needed(
        self,
        node_id: str,
        state: dict,
        depth: int,
        threshold: int,
    ) -> None:
        """
        If depth > threshold, write a full checkpoint state_delta onto node_id.
        The checkpoint carries all current state keys plus "_checkpoint": true.
        """
        if depth <= threshold:
            return

        checkpoint: dict = {"_checkpoint": True, **state}
        self.update_node_state_delta(node_id, json.dumps(checkpoint, ensure_ascii=False))
        logger.debug(
            "Checkpoint written on node %s (walk depth was %d)", node_id, depth
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

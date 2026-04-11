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
  .get_ancestors(node_id)     — [root, ..., node] order
  .get_children(node_id)      — direct children (unordered)
  .get_root()                 — the single global root node
  .close()                    — close the connection
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Exhaustive set of roles the dialogue layer is allowed to write.
# Anything outside this set is rejected at the DB boundary so malformed
# or injected role strings can never corrupt assembly logic.
_VALID_ROLES = {"user", "assistant", "system", "tool"}


# ---------------------------------------------------------------------------
# Node dataclass
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id:           str
    parent_id:    str | None
    role:         str            # user | assistant | system | tool
    content:      str            # JSON if list (attachment blocks), else plain str
    created_at:   float          # unix timestamp
    tool_calls:   str | None     # JSON or None
    tool_call_id: str | None     # for tool-result nodes
    author_id:    str | None     # group chat sender; None otherwise


# ---------------------------------------------------------------------------
# ConversationDB
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    tool_calls   TEXT,
    tool_call_id TEXT,
    author_id    TEXT,
    FOREIGN KEY (parent_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_INSERT_NODE = """
INSERT INTO nodes (id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_NODE = "SELECT id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id FROM nodes WHERE id = ?"

# Recursive CTE: walks from node up to root, then we reverse in Python.
_ANCESTORS_CTE = """
WITH RECURSIVE anc AS (
    SELECT id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id
    FROM nodes WHERE id = ?
    UNION ALL
    SELECT n.id, n.parent_id, n.role, n.content, n.created_at, n.tool_calls, n.tool_call_id, n.author_id
    FROM nodes n JOIN anc a ON n.id = a.parent_id
)
SELECT id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id FROM anc
"""

_CHILDREN = "SELECT id, parent_id, role, content, created_at, tool_calls, tool_call_id, author_id FROM nodes WHERE parent_id = ? ORDER BY created_at"


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
    )


class ConversationDB:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create tables and the global root node if they don't exist yet."""
        # Run schema DDL inside an explicit transaction rather than via
        # executescript(), which issues an implicit COMMIT first and would
        # disable the WAL/foreign-key PRAGMAs set in __init__.
        with self._conn:
            self._conn.executescript(_SCHEMA)
        # Insert root node if not already present
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'root_id'").fetchone()
        if row is None:
            root_id = str(uuid.uuid4())
            self._conn.execute(
                _INSERT_NODE,
                (root_id, None, "system", "", time.time(), None, None, None),
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
    ) -> Node:
        # Validate role against the allowlist — all queries are already
        # parameterized so there is no SQLi risk, but an invalid role would
        # corrupt context assembly and is a sign something has gone wrong.
        if role not in _VALID_ROLES:
            raise ValueError(
                f"ConversationDB.add_node: invalid role {role!r}. "
                f"Must be one of {sorted(_VALID_ROLES)}."
            )
        # parent_id must be a non-empty string — only the global root node
        # (written by ensure_schema) is allowed to have parent_id=None.
        if not parent_id or not isinstance(parent_id, str):
            raise ValueError(
                "ConversationDB.add_node: parent_id must be a non-empty string. "
                "Only the global root node may have parent_id=None."
            )
        node_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            _INSERT_NODE,
            (node_id, parent_id, role, content, now, tool_calls, tool_call_id, author_id),
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
        )

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute(_SELECT_NODE, (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def get_ancestors(self, node_id: str) -> list[Node]:
        """
        Return the ancestor chain in root → node order (inclusive of node_id).
        The root node itself is excluded — it's a structural placeholder with
        no dialogue content.
        """
        rows = self._conn.execute(_ANCESTORS_CTE, (node_id,)).fetchall()
        nodes = [_row_to_node(r) for r in rows]
        # CTE walks child → root; reverse to get root → child order.
        nodes.reverse()
        # Drop the global root (parent_id is None, role=system, content="")
        # so it doesn't appear as an empty system turn in dialogue assembly.
        if nodes and nodes[0].parent_id is None and nodes[0].content == "":
            nodes = nodes[1:]
        # Drop session-init nodes (role=system, content starts with "session:")
        # — these are structural branch anchors, not dialogue content.
        nodes = [n for n in nodes if not (n.role == "system" and n.content.startswith("session:"))]
        return nodes

    def get_children(self, node_id: str) -> list[Node]:
        rows = self._conn.execute(_CHILDREN, (node_id,)).fetchall()
        return [_row_to_node(r) for r in rows]

    def update_node_content(self, node_id: str, content: str) -> bool:
        """Update a node's content in-place. Returns True if found."""
        cur = self._conn.execute(
            "UPDATE nodes SET content = ? WHERE id = ?", (content, node_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_node(self, node_id: str) -> bool:
        """Delete a single node row. Does NOT cascade — callers handle dependents."""
        cur = self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

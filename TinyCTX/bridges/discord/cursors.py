"""
bridges/discord/cursors.py — Session cursor persistence for the Discord bridge.

Persists two JSON files under data/cursors/ (not workspace/ — this is
bridge-internal bookkeeping, not agent-authored content):

  discord.json              cursor_key -> node_id
                            Keys: "dm:<uid>", "group:<cid>", "thread:<tid>"

  discord_msg_nodes.json    discord_message_id -> db_node_id
                            Records which DB node a channel trigger message
                            produced, so thread forks can branch accurately.
                            Capped at MAX_MSG_NODES entries (LRU-style trim).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class CursorStore:
    """
    Persists Discord bridge session cursors and message→node mappings across
    bot restarts. Backed by two JSON files in data/cursors/.
    """

    MAX_MSG_NODES = 2000

    def __init__(self, cursors_dir: Path) -> None:
        self._dir           = cursors_dir
        self._cursor_file   = cursors_dir / "discord.json"
        self._msg_node_file = cursors_dir / "discord_msg_nodes.json"
        self._cursors:   dict[str, str] = self._load(self._cursor_file)
        self._msg_nodes: dict[str, str] = self._load(self._msg_node_file)

    # ------------------------------------------------------------------
    # Cursor map (cursor_key -> node_id)
    # ------------------------------------------------------------------

    def get(self, cursor_key: str) -> str | None:
        return self._cursors.get(cursor_key)

    def set(self, cursor_key: str, node_id: str) -> None:
        self._cursors[cursor_key] = node_id
        self._save(self._cursor_file, self._cursors)

    def delete(self, cursor_key: str) -> None:
        self._cursors.pop(cursor_key, None)
        self._save(self._cursor_file, self._cursors)

    def all_cursors(self) -> dict[str, str]:
        return dict(self._cursors)

    # ------------------------------------------------------------------
    # Reconciliation — drop cursors pointing at nodes that no longer
    # exist in the DB (e.g. agent.db was deleted/replaced out from under
    # the bot, so old node_ids are now dangling).
    # ------------------------------------------------------------------

    def reconcile(self, db) -> None:
        """Drop any cursor / msg-node entries whose node_id is missing from db.

        Safe to call on every startup: cheap when the DB is intact (all
        lookups hit), and self-heals to a blank cursor state when the DB
        was wiped, instead of letting stale node_ids blow up later as FK
        errors on the first turn.
        """
        stale_cursors = [k for k, node_id in self._cursors.items() if db.get_node(node_id) is None]
        for k in stale_cursors:
            del self._cursors[k]
        if stale_cursors:
            logger.warning(
                "CursorStore: dropped %d stale cursor(s) pointing at missing nodes: %s",
                len(stale_cursors), stale_cursors,
            )
            self._save(self._cursor_file, self._cursors)

        stale_msg_nodes = [k for k, node_id in self._msg_nodes.items() if db.get_node(node_id) is None]
        for k in stale_msg_nodes:
            del self._msg_nodes[k]
        if stale_msg_nodes:
            logger.warning(
                "CursorStore: dropped %d stale msg-node mapping(s) pointing at missing nodes",
                len(stale_msg_nodes),
            )
            self._save(self._msg_node_file, self._msg_nodes)

    # ------------------------------------------------------------------
    # Message → node map (discord_message_id -> db_node_id)
    # ------------------------------------------------------------------

    def get_msg_node(self, discord_message_id: str) -> str | None:
        return self._msg_nodes.get(discord_message_id)

    def set_msg_node(self, discord_message_id: str, node_id: str) -> None:
        self._msg_nodes[discord_message_id] = node_id
        # Trim to cap if needed (remove oldest entries).
        if len(self._msg_nodes) > self.MAX_MSG_NODES:
            overflow = len(self._msg_nodes) - self.MAX_MSG_NODES
            for key in list(self._msg_nodes.keys())[:overflow]:
                del self._msg_nodes[key]
        self._save(self._msg_node_file, self._msg_nodes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load(path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning(
                    "CursorStore: corrupt file %s — starting fresh", path
                )
        return {}

    @staticmethod
    def _save(path: Path, data: dict) -> None:
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("CursorStore: failed to save %s", path)


def make_session_node(db, cursor_key: str) -> str:
    """Create a new session-anchor node off the global root and return its id."""
    root = db.get_root()
    node = db.add_node(parent_id=root.id, role="system", content=f"session:{cursor_key}")
    return node.id

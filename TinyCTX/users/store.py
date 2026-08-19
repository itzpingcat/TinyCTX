from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path

import platformdirs

from TinyCTX.contracts import Platform
from TinyCTX.permissions import Permission
from TinyCTX.users.models import PlatformIdentity, User

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class UsernameConflictError(Exception):
    pass


# ---------------------------------------------------------------------------
# Wordlists for random username generation
# ---------------------------------------------------------------------------

_ADJECTIVES = [
    "amber", "azure", "bold", "bright", "calm", "crisp", "dawn", "deep",
    "deft", "dusk", "early", "fair", "fast", "glad", "gold", "grand",
    "grey", "high", "keen", "kind", "lark", "lean", "light", "lone",
    "mild", "mint", "mist", "neat", "nord", "open", "pale", "pine",
    "pure", "quick", "rare", "rich", "sage", "salt", "slim", "soft",
    "star", "still", "stone", "storm", "stout", "surf", "swift", "tall",
    "teal", "thin", "tide", "trim", "true", "vale", "vast", "warm",
    "west", "wild", "wind", "wise", "wren",
]

_NOUNS = [
    "ash", "bay", "bear", "birch", "brook", "buck", "cloud", "cove",
    "crane", "creek", "crow", "dale", "deer", "dove", "dune", "elk",
    "fern", "finch", "fjord", "flock", "ford", "fox", "glen", "gull",
    "hawk", "haze", "hill", "jade", "jay", "kite", "lake", "lark",
    "leaf", "lynx", "marsh", "mead", "mink", "moor", "moss", "moth",
    "oak", "otter", "owl", "peak", "pine", "pond", "quail", "rail",
    "reed", "ridge", "rook", "rune", "rush", "skye", "slate", "sparrow",
    "spire", "spring", "starling", "stone", "storm", "swallow", "swift",
    "teal", "tern", "tide", "vale", "vole", "wave", "wren", "yarrow",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(s: str) -> str:
    """Lowercase, keep alphanumeric/hyphens/underscores, truncate to 32 chars."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s[:32]


def _random_username() -> str:
    adj  = random.choice(_ADJECTIVES)
    noun = random.choice(_NOUNS)
    num  = random.randint(1000, 9999)
    return f"{adj}-{noun}-{num}"


def _identity_to_dict(identity: PlatformIdentity) -> dict:
    return {
        "platform":     identity.platform.value,
        "user_id":      identity.user_id,
        "username":     identity.username,
        "display_name": identity.display_name,
    }


def _identity_from_dict(d: dict) -> PlatformIdentity:
    return PlatformIdentity(
        platform=Platform(d["platform"]),
        user_id=d["user_id"],
        username=d["username"],
        display_name=d["display_name"],
    )


def _parse_overrides(raw: str, username: str) -> dict[str, bool]:
    """
    json.loads the stored permission_overrides column, tolerating garbage
    rather than making a row unloadable. Per-key unknown-permission
    dropping happens later, in User.effective_permissions() — this only
    guards against the column itself being corrupt/non-dict JSON.
    """
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("users: corrupt permission_overrides for %r, ignoring", username)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("users: permission_overrides for %r is not an object, ignoring", username)
        return {}
    return {str(k): bool(v) for k, v in parsed.items()}


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        username=row["username"],
        identities=[_identity_from_dict(d) for d in json.loads(row["identities"])],
        meta=json.loads(row["meta"]),
        created_at=row["created_at"],
        permission_overrides=_parse_overrides(row["permission_overrides"], row["username"]),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    username             TEXT PRIMARY KEY,
    permission_overrides TEXT NOT NULL DEFAULT '{}',
    identities           TEXT NOT NULL DEFAULT '[]',
    meta                  TEXT NOT NULL DEFAULT '{}',
    created_at            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_platform_index (
    platform TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    username TEXT NOT NULL REFERENCES users(username),
    PRIMARY KEY (platform, user_id)
);
"""

# ---------------------------------------------------------------------------
# Migration-only history: the named tiers this codebase briefly had (first
# as permission_level int ranges, then as a permission_template column)
# before the plan was simplified to a single global template. Frozen here
# ONLY so an existing users.db from either era can be converted without
# changing what its users could already do — TinyCTX/config.py no longer
# defines named templates at all, and nothing outside _migrate() below
# should reference this dict. See docs/PERMISSIONS-PLAN.md §2's migration
# note.
_LEGACY_TEMPLATES: dict[str, frozenset[Permission]] = {
    "guest": frozenset(),
    "member": frozenset({
        Permission.FILE_READ, Permission.NETWORK_READ, Permission.MEMORY_READ,
    }),
    "trusted": frozenset({
        Permission.FILE_READ, Permission.FILE_WRITE,
        Permission.NETWORK_READ, Permission.NETWORK_WRITE,
        Permission.MEMORY_READ, Permission.MEMORY_WRITE,
        Permission.MANAGE_CTX, Permission.MODEL_SWAP,
        Permission.CRON_CREATE, Permission.DM_ACCESS,
        Permission.USER_READ, Permission.IMAGE_GEN,
    }),
    "operator": frozenset(Permission),
}

# permission_level -> legacy template name, per the original
# docs/PERMISSIONS-PLAN.md §2.1. Ranges are inclusive of their low bound;
# checked in ascending order. Migration-only, same as _LEGACY_TEMPLATES.
_BACKFILL_RANGES: list[tuple[int, str]] = [
    (0, "guest"),
    (25, "member"),
    (50, "trusted"),
    (90, "operator"),
]


def _template_name_for_level(level: int) -> str:
    name = "guest"
    for lo, candidate in _BACKFILL_RANGES:
        if level >= lo:
            name = candidate
    return name


def _sparse_overrides_for(
    perms: frozenset[Permission], template: frozenset[Permission]
) -> dict[str, bool]:
    """
    Diff a resolved legacy permission set against the CURRENT global
    permissions.template, keeping only the entries that differ — the same
    sparse shape User.effective_permissions() expects everywhere else.

    Used only when migrating a row off permission_template or
    permission_level. `template` is the live PermissionsConfig.template,
    passed into UserStore at construction time (see __init__'s `template`
    param) — UserStore itself still doesn't import TinyCTX.config, it just
    receives the already-resolved frozenset as plain data, same layering
    rule as before. Diffing against the real template (instead of pinning
    every bool explicitly) means a migrated user's overrides shrink to just
    their actual deltas, and later template changes apply to them like any
    other user instead of being masked by a frozen full-dict snapshot.
    """
    return {p.value: (p in perms) for p in Permission if (p in perms) != (p in template)}


# ---------------------------------------------------------------------------
# UserStore
# ---------------------------------------------------------------------------

class UserStore:
    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        template: frozenset[Permission] = frozenset(),
    ) -> None:
        """
        data_dir: directory to store users.db in. Should be the instance's
        internal data dir (Config.data.path), NOT the workspace — keeps
        per-instance user data isolated so multiple TinyCTX instances don't
        share one users.db.

        Falls back to TINYCTX_DATA_PATH env var, then platformdirs, only
        when data_dir is not supplied (back-compat for callers that haven't
        been updated yet). TINYCTX_DATA_PATH and the old TINYCTX_CONFIG_DIR
        point at the same directory now that config-dir and data-dir are the
        same concept — there is no separate config dir anymore.

        template: the resolved permissions.template frozenset (from
        Config.permissions.template), used ONLY by _migrate() to diff a
        legacy user's resolved permissions down to a sparse overrides dict
        instead of pinning every bool. Passed as plain data — UserStore
        still does not import TinyCTX.config, preserving the layering
        _sparse_overrides_for's docstring describes. Defaults to empty
        (fail-closed) for callers that don't pass one; a legacy migration
        run under the default will treat "no template" as the diff base,
        which only matters for users.db files still carrying
        permission_level/permission_template columns.
        """
        self._template = template
        if data_dir is not None:
            config_dir = Path(data_dir)
        else:
            config_dir_env = os.environ.get("TINYCTX_DATA_PATH", "") or os.environ.get("TINYCTX_CONFIG_DIR", "")
            if config_dir_env:
                config_dir = Path(config_dir_env)
            else:
                config_dir = Path(platformdirs.user_config_dir("tinyctx"))
        config_dir.mkdir(parents=True, exist_ok=True)
        db_path = config_dir / "users.db"
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._migrate(db_path)
        self._conn.commit()
        logger.info("UserStore: db at %s", db_path)

        # LRU caches (simple dicts — invalidated on write)
        self._cache_by_platform: dict[tuple[str, str], User] = {}  # (platform.value, user_id) -> User
        self._cache_by_username: dict[str, User] = {}              # username -> User

    def _migrate(self, db_path: Path) -> None:
        """
        Migration guard, same PRAGMA table_info + ALTER TABLE ADD COLUMN
        pattern used by modules/cron's cron_jobs.run_in column
        (CREATE TABLE IF NOT EXISTS is a no-op against an existing table
        with an older column set).

        Three things can be true of an existing users.db, in the order this
        codebase actually produced them:
          1. Oldest — only the original permission_level INTEGER column.
             Resolve each user's legacy-template permissions, diff against
             the live permissions.template (self._template, via
             _sparse_overrides_for) into a sparse permission_overrides dict,
             then drop permission_level.
          2. Short-lived middle state — has permission_template AND
             permission_overrides (the multi-template system this file
             briefly implemented). Resolve template + overrides into one
             set the same way User.effective_permissions() used to, diff
             that against the live permissions.template the same way as
             branch 1, write the sparse result back as permission_overrides,
             then drop permission_template.
          3. Current — only permission_overrides. Nothing to do.
        These are checked as elif, not independently: by the time state 2
        existed, state 1 had already been fully migrated into it (see the
        original migration this replaces), so a real users.db is never in
        both branches' preconditions at once.
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(users)")}

        if "permission_overrides" not in cols:
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN permission_overrides TEXT NOT NULL DEFAULT '{}'"
            )
            cols.add("permission_overrides")

        if "permission_template" in cols:
            logger.warning(
                "UserStore: %s has a legacy permission_template column (the "
                "short-lived multi-template system) — freezing each user's "
                "resolved permissions into permission_overrides and "
                "dropping it.", db_path,
            )
            rows = self._conn.execute(
                "SELECT username, permission_template, permission_overrides FROM users"
            ).fetchall()
            for row in rows:
                resolved = set(_LEGACY_TEMPLATES.get(row["permission_template"] or "guest", frozenset()))
                for name, value in _parse_overrides(row["permission_overrides"], row["username"]).items():
                    try:
                        perm = Permission(name)
                    except ValueError:
                        continue
                    if value:
                        resolved.add(perm)
                    else:
                        resolved.discard(perm)
                self._conn.execute(
                    "UPDATE users SET permission_overrides = ? WHERE username = ?",
                    (
                        json.dumps(_sparse_overrides_for(frozenset(resolved), self._template)),
                        row["username"],
                    ),
                )
            self._conn.commit()
            try:
                self._conn.execute("ALTER TABLE users DROP COLUMN permission_template")
            except sqlite3.OperationalError:
                # SQLite < 3.35 can't drop columns. Harmless to leave it —
                # nothing reads it anymore — but log so an operator on an
                # ancient SQLite knows the column is stale, not live.
                logger.warning(
                    "UserStore: could not DROP COLUMN permission_template "
                    "(SQLite too old) — column left in place, unused."
                )

        elif "permission_level" in cols:
            logger.warning(
                "UserStore: %s has a legacy permission_level column — "
                "freezing each user's resolved permissions into "
                "permission_overrides and dropping it.", db_path,
            )
            rows = self._conn.execute(
                "SELECT username, permission_level FROM users"
            ).fetchall()
            for row in rows:
                name = _template_name_for_level(row["permission_level"])
                resolved = _LEGACY_TEMPLATES.get(name, frozenset())
                self._conn.execute(
                    "UPDATE users SET permission_overrides = ? WHERE username = ?",
                    (
                        json.dumps(_sparse_overrides_for(frozenset(resolved), self._template)),
                        row["username"],
                    ),
                )
            self._conn.commit()
            try:
                self._conn.execute("ALTER TABLE users DROP COLUMN permission_level")
            except sqlite3.OperationalError:
                logger.warning(
                    "UserStore: could not DROP COLUMN permission_level "
                    "(SQLite too old) — column left in place, unused."
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_user(
        self,
        platform: Platform,
        user_id: str,
        username: str,
        display_name: str,
    ) -> User:
        """
        Hot path — called by every bridge on every inbound message.
        Lookup by (platform, user_id). Create if not found.
        Update stored identity if username/display_name changed.
        """
        cache_key = (platform.value, user_id)

        # 1. LRU cache hit
        if cache_key in self._cache_by_platform:
            user = self._cache_by_platform[cache_key]
            user = self._maybe_update_identity(user, platform, user_id, username, display_name)
            return user

        # 2. DB lookup
        row = self._conn.execute(
            "SELECT username FROM user_platform_index WHERE platform = ? AND user_id = ?",
            (platform.value, user_id),
        ).fetchone()

        if row:
            user = self._load_user(row["username"])
            if user is None:
                # Index points to a deleted user — treat as new
                user = self._create_user(platform, user_id, username, display_name)
            else:
                user = self._maybe_update_identity(user, platform, user_id, username, display_name)
            self._cache_by_platform[cache_key] = user
            self._cache_by_username[user.username] = user
            return user

        # 3. Not found — create
        user = self._create_user(platform, user_id, username, display_name)
        return user

    def create_user(
        self,
        username: str,
        *,
        platform: Platform = Platform.CLI,
    ) -> User:
        """
        Create a user with the exact requested username — no slugify/random
        fallback (unlike resolve_user's internal _create_user, which picks a
        username for a platform identity that may already be taken).

        Used by the CLI launch flow when a typed-in username doesn't exist
        yet. Raises UsernameConflictError if the username is already taken.

        Every new user starts with empty permission_overrides — there is a
        single global permissions.template (config.yaml) now, so there is
        nothing to select at create time. Grant this user more (or less)
        afterward by editing their overrides.
        """
        if self._username_taken(username):
            raise UsernameConflictError(f"Username already taken: {username!r}")

        identity = PlatformIdentity(
            platform=platform,
            user_id=username,
            username=username,
            display_name=username,
        )
        user = User(
            username=username,
            identities=[identity],
            meta={},
            created_at=time.time(),
            permission_overrides={},
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO users (username, permission_overrides, identities, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user.username,
                    json.dumps(user.permission_overrides),
                    json.dumps([_identity_to_dict(identity)]),
                    json.dumps(user.meta),
                    user.created_at,
                ),
            )
            self._conn.execute(
                "INSERT INTO user_platform_index (platform, user_id, username) VALUES (?, ?, ?)",
                (platform.value, username, username),
            )
        self._populate_cache(user)
        logger.info("UserStore: created user %r via %s (exact)", username, platform.value)
        return user

    def get_user(self, username: str) -> User | None:
        if username in self._cache_by_username:
            return self._cache_by_username[username]
        return self._load_user(username)

    def get_by_platform(self, platform: Platform, user_id: str) -> User | None:
        """Read-only lookup by platform identity. Returns None if not found."""
        cache_key = (platform.value, user_id)
        if cache_key in self._cache_by_platform:
            return self._cache_by_platform[cache_key]
        row = self._conn.execute(
            "SELECT username FROM user_platform_index WHERE platform = ? AND user_id = ?",
            (platform.value, user_id),
        ).fetchone()
        if not row:
            return None
        return self._load_user(row["username"])

    def update_user(self, user: User) -> None:
        self._conn.execute(
            "UPDATE users SET permission_overrides = ?, identities = ?, meta = ? WHERE username = ?",
            (
                json.dumps(user.permission_overrides),
                json.dumps([_identity_to_dict(i) for i in user.identities]),
                json.dumps(user.meta),
                user.username,
            ),
        )
        self._conn.commit()
        self._invalidate(user)

    def merge_users(self, primary_username: str, secondary_username: str) -> User:
        primary   = self._load_user(primary_username)
        secondary = self._load_user(secondary_username)
        if primary is None:
            raise ValueError(f"User not found: {primary_username!r}")
        if secondary is None:
            raise ValueError(f"User not found: {secondary_username!r}")

        # Merge identities — deduplicate by (platform, user_id)
        existing = {(i.platform, i.user_id) for i in primary.identities}
        for ident in secondary.identities:
            if (ident.platform, ident.user_id) not in existing:
                primary.identities.append(ident)

        with self._conn:
            self._conn.execute(
                "UPDATE user_platform_index SET username = ? WHERE username = ?",
                (primary_username, secondary_username),
            )
            self._conn.execute(
                "UPDATE users SET identities = ? WHERE username = ?",
                (json.dumps([_identity_to_dict(i) for i in primary.identities]), primary_username),
            )
            self._conn.execute("DELETE FROM users WHERE username = ?", (secondary_username,))

        self._invalidate_by_username(secondary_username)
        self._invalidate(primary)

        user = self._load_user(primary_username)
        assert user is not None
        self._populate_cache(user)
        return user

    def rename_user(self, username: str, new_username: str) -> User:
        if self._username_taken(new_username):
            raise UsernameConflictError(f"Username already taken: {new_username!r}")

        with self._conn:
            self._conn.execute(
                "UPDATE user_platform_index SET username = ? WHERE username = ?",
                (new_username, username),
            )
            self._conn.execute(
                "UPDATE users SET username = ? WHERE username = ?",
                (new_username, username),
            )

        self._invalidate_by_username(username)

        user = self._load_user(new_username)
        assert user is not None
        self._populate_cache(user)
        return user

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_user(self, username: str) -> User | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            return None
        user = _user_from_row(row)
        self._cache_by_username[username] = user
        return user

    def _create_user(
        self,
        platform: Platform,
        user_id: str,
        username: str,
        display_name: str,
    ) -> User:
        new_username = self._pick_username(username, display_name)
        identity     = PlatformIdentity(
            platform=platform,
            user_id=user_id,
            username=username,
            display_name=display_name,
        )
        user = User(
            username=new_username,
            identities=[identity],
            meta={},
            created_at=time.time(),
            permission_overrides={},  # resolves against permissions.template
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO users (username, permission_overrides, identities, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user.username,
                    json.dumps(user.permission_overrides),
                    json.dumps([_identity_to_dict(identity)]),
                    json.dumps(user.meta),
                    user.created_at,
                ),
            )
            self._conn.execute(
                "INSERT INTO user_platform_index (platform, user_id, username) VALUES (?, ?, ?)",
                (platform.value, user_id, new_username),
            )
        self._populate_cache(user)
        logger.info("UserStore: created user %r for %s/%s", new_username, platform.value, user_id)
        return user

    def _maybe_update_identity(
        self,
        user: User,
        platform: Platform,
        user_id: str,
        username: str,
        display_name: str,
    ) -> User:
        """
        If the platform identity's username or display_name has changed,
        update it and persist.
        """
        for i, ident in enumerate(user.identities):
            if ident.platform == platform and ident.user_id == user_id:
                if ident.username != username or ident.display_name != display_name:
                    user.identities[i] = PlatformIdentity(
                        platform=platform,
                        user_id=user_id,
                        username=username,
                        display_name=display_name,
                    )
                    self.update_user(user)
                    logger.debug(
                        "UserStore: updated identity for %r on %s", user.username, platform.value
                    )
                return user
        return user

    def _pick_username(self, platform_username: str, display_name: str) -> str:
        for candidate in [_slugify(platform_username), _slugify(display_name)]:
            if candidate and not self._username_taken(candidate):
                return candidate
        while True:
            candidate = _random_username()
            if not self._username_taken(candidate):
                return candidate

    def _username_taken(self, username: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row is not None

    def _invalidate(self, user: User) -> None:
        self._cache_by_username.pop(user.username, None)
        for ident in user.identities:
            self._cache_by_platform.pop((ident.platform.value, ident.user_id), None)

    def _invalidate_by_username(self, username: str) -> None:
        user = self._cache_by_username.pop(username, None)
        if user:
            for ident in user.identities:
                self._cache_by_platform.pop((ident.platform.value, ident.user_id), None)

    def _populate_cache(self, user: User) -> None:
        self._cache_by_username[user.username] = user
        for ident in user.identities:
            self._cache_by_platform[(ident.platform.value, ident.user_id)] = user

"""
tests/test_users_store.py

Tests for users/store.py (UserStore) and users/models.py (User, PlatformIdentity).
Covers user creation, resolve_user idempotency, username generation/uniqueness
(_slugify, _random_username), rename/merge conflict behavior, persistence
across store reopen with the same data_dir, and the permission_overrides
column that's now the ONLY per-user permission state (see
TinyCTX/permissions.py and docs/PERMISSIONS-PLAN.md §2) — round-trip,
corrupt-column tolerance, unknown-key dropping, and the two-stage legacy
migration guard (permission_level int -> permission_overrides directly, and
the short-lived permission_template + permission_overrides middle state ->
permission_overrides).

Run with:
    pytest tests/
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from TinyCTX.config import PermissionsConfig
from TinyCTX.contracts import Platform
from TinyCTX.permissions import Permission
from TinyCTX.users.models import PlatformIdentity, User
from TinyCTX.users.store import (
    UserStore,
    UsernameConflictError,
    _LEGACY_TEMPLATES,
    _explicit_overrides_for,
    _random_username,
    _slugify,
    _template_name_for_level,
)


@pytest.fixture
def store(tmp_path):
    s = UserStore(data_dir=tmp_path)
    return s


# ---------------------------------------------------------------------------
# resolve_user — creation and idempotency
# ---------------------------------------------------------------------------

class TestResolveUser:
    def test_creates_new_user(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        assert isinstance(user, User)
        assert user.username
        # Empty overrides -> resolves entirely against the single global
        # permissions.template, nothing baked in at creation time.
        assert user.permission_overrides == {}
        assert len(user.identities) == 1
        assert user.identities[0].platform == Platform.DISCORD
        assert user.identities[0].user_id == "u1"

    def test_same_identity_resolves_to_same_user(self, store):
        a = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        b = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        assert a.username == b.username

    def test_same_identity_resolves_to_same_user_via_db_not_cache(self, tmp_path):
        s1 = UserStore(data_dir=tmp_path)
        created = s1.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")

        s2 = UserStore(data_dir=tmp_path)  # fresh store, empty cache
        found = s2.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        assert found.username == created.username

    def test_different_user_ids_create_different_users(self, store):
        a = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        b = store.resolve_user(Platform.DISCORD, "u2", "bob", "Bob")
        assert a.username != b.username

    def test_same_user_id_different_platforms_create_different_users(self, store):
        a = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        b = store.resolve_user(Platform.TELEGRAM, "u1", "alice", "Alice")
        assert a.username != b.username

    def test_updates_identity_on_username_change(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        updated = store.resolve_user(Platform.DISCORD, "u1", "alice2", "Alice")
        assert updated.username == user.username  # TinyCTX username unchanged
        ident = updated.identities[0]
        assert ident.username == "alice2"

    def test_updates_identity_on_display_name_change(self, store):
        store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        updated = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice Smith")
        assert updated.identities[0].display_name == "Alice Smith"

    def test_no_update_when_identity_unchanged(self, store):
        a = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        b = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        assert a.identities[0] == b.identities[0]


# ---------------------------------------------------------------------------
# get_user / get_by_platform
# ---------------------------------------------------------------------------

class TestGetters:
    def test_get_user_returns_created_user(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        fetched = store.get_user(user.username)
        assert fetched is not None
        assert fetched.username == user.username

    def test_get_user_missing_returns_none(self, store):
        assert store.get_user("nonexistent-user") is None

    def test_get_by_platform_returns_user(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        fetched = store.get_by_platform(Platform.DISCORD, "u1")
        assert fetched is not None
        assert fetched.username == user.username

    def test_get_by_platform_missing_returns_none(self, store):
        assert store.get_by_platform(Platform.DISCORD, "nonexistent") is None


# ---------------------------------------------------------------------------
# create_user — exact username, no per-user template selection
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_create_user_exact_username(self, store):
        user = store.create_user("exact-name")
        assert user.username == "exact-name"
        assert user.permission_overrides == {}

    def test_create_user_conflict_raises(self, store):
        store.create_user("taken")
        with pytest.raises(UsernameConflictError):
            store.create_user("taken")

    def test_create_user_takes_no_template_kwarg(self, store):
        """There's a single global template now — create_user() has nothing
        to select at creation time."""
        import inspect
        params = inspect.signature(store.create_user).parameters
        assert "permission_template" not in params


# ---------------------------------------------------------------------------
# Username generation: _slugify / _random_username / uniqueness
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_lowercases(self):
        assert _slugify("ALICE") == "alice"

    def test_strips_invalid_chars(self):
        assert _slugify("alice smith!@#") == "alicesmith"

    def test_keeps_hyphens_and_underscores(self):
        assert _slugify("alice-smith_99") == "alice-smith_99"

    def test_truncates_to_32_chars(self):
        long_name = "a" * 50
        result = _slugify(long_name)
        assert len(result) == 32

    def test_empty_string(self):
        assert _slugify("") == ""

    def test_all_invalid_chars_yields_empty(self):
        assert _slugify("!!!@@@") == ""


class TestRandomUsername:
    def test_format(self):
        name = _random_username()
        parts = name.split("-")
        assert len(parts) == 3
        assert parts[2].isdigit()
        assert 1000 <= int(parts[2]) <= 9999


class TestUsernameUniqueness:
    def test_username_derived_from_platform_username(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "Alice", "Alice Smith")
        assert user.username == "alice"

    def test_conflicting_slugified_username_falls_back(self, store):
        first = store.resolve_user(Platform.DISCORD, "u1", "Alice", "Someone")
        assert first.username == "alice"
        # Second user has the same platform username "Alice" -> slugifies to
        # "alice" which is taken, so it must fall back to display_name slug.
        second = store.resolve_user(Platform.TELEGRAM, "u2", "Alice", "Alice Display")
        assert second.username != first.username
        assert second.username == "alicedisplay"

    def test_all_candidates_conflict_falls_back_to_random(self, store):
        store.resolve_user(Platform.DISCORD, "u1", "dupe", "dupe")
        second = store.resolve_user(Platform.TELEGRAM, "u2", "dupe", "dupe")
        # Both slug candidates ("dupe") are taken -> random wordlist username.
        assert second.username != "dupe"
        parts = second.username.split("-")
        assert len(parts) == 3
        assert parts[2].isdigit()

    def test_created_users_have_unique_usernames(self, store):
        usernames = set()
        for i in range(20):
            u = store.resolve_user(Platform.DISCORD, f"u{i}", "samename", "samename")
            usernames.add(u.username)
        assert len(usernames) == 20


# ---------------------------------------------------------------------------
# update_user
# ---------------------------------------------------------------------------

class TestUpdateUser:
    def test_update_user_persists_meta(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        user.meta["key"] = "value"
        store.update_user(user)
        fetched = store.get_user(user.username)
        assert fetched.meta["key"] == "value"

    def test_update_user_persists_permission_overrides(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        user.permission_overrides = {"file_write": True, "network_read": False}
        store.update_user(user)
        fetched = store.get_user(user.username)
        assert fetched.permission_overrides == {"file_write": True, "network_read": False}


# ---------------------------------------------------------------------------
# rename_user — UsernameConflictError
# ---------------------------------------------------------------------------

class TestRenameUser:
    def test_rename_success(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        renamed = store.rename_user(user.username, "new-alice")
        assert renamed.username == "new-alice"
        assert store.get_user("new-alice") is not None
        assert store.get_user(user.username) is None

    def test_rename_to_taken_username_raises(self, store):
        store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        bob = store.resolve_user(Platform.TELEGRAM, "u2", "bob", "Bob")
        with pytest.raises(UsernameConflictError):
            store.rename_user(bob.username, "alice")

    def test_rename_updates_platform_index(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        store.rename_user(user.username, "renamed")
        fetched = store.get_by_platform(Platform.DISCORD, "u1")
        assert fetched is not None
        assert fetched.username == "renamed"


# ---------------------------------------------------------------------------
# merge_users
# ---------------------------------------------------------------------------

class TestMergeUsers:
    def test_merge_combines_identities(self, store):
        primary = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        secondary = store.resolve_user(Platform.TELEGRAM, "u2", "alice_tg", "Alice")
        merged = store.merge_users(primary.username, secondary.username)
        platforms = {i.platform for i in merged.identities}
        assert platforms == {Platform.DISCORD, Platform.TELEGRAM}

    def test_merge_removes_secondary_user(self, store):
        primary = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        secondary = store.resolve_user(Platform.TELEGRAM, "u2", "alice_tg", "Alice")
        store.merge_users(primary.username, secondary.username)
        assert store.get_user(secondary.username) is None

    def test_merge_secondary_identity_resolves_to_primary(self, store):
        primary = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        secondary = store.resolve_user(Platform.TELEGRAM, "u2", "alice_tg", "Alice")
        store.merge_users(primary.username, secondary.username)
        fetched = store.get_by_platform(Platform.TELEGRAM, "u2")
        assert fetched is not None
        assert fetched.username == primary.username

    def test_merge_missing_primary_raises(self, store):
        secondary = store.resolve_user(Platform.TELEGRAM, "u2", "alice_tg", "Alice")
        with pytest.raises(ValueError):
            store.merge_users("nonexistent", secondary.username)

    def test_merge_missing_secondary_raises(self, store):
        primary = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        with pytest.raises(ValueError):
            store.merge_users(primary.username, "nonexistent")


# ---------------------------------------------------------------------------
# Persistence across reopen
# ---------------------------------------------------------------------------

class TestPersistenceAcrossReopen:
    def test_user_persists_after_reopen(self, tmp_path):
        s1 = UserStore(data_dir=tmp_path)
        user = s1.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")

        s2 = UserStore(data_dir=tmp_path)
        fetched = s2.get_user(user.username)
        assert fetched is not None
        assert fetched.username == user.username
        assert fetched.identities[0].user_id == "u1"

    def test_updated_meta_persists_after_reopen(self, tmp_path):
        s1 = UserStore(data_dir=tmp_path)
        user = s1.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        user.meta["foo"] = "bar"
        s1.update_user(user)

        s2 = UserStore(data_dir=tmp_path)
        fetched = s2.get_user(user.username)
        assert fetched.meta["foo"] == "bar"


# ---------------------------------------------------------------------------
# models.py — plain dataclasses
# ---------------------------------------------------------------------------

class TestModels:
    def test_platform_identity_fields(self):
        ident = PlatformIdentity(
            platform=Platform.DISCORD, user_id="1", username="a", display_name="A"
        )
        assert ident.platform == Platform.DISCORD
        assert ident.user_id == "1"
        assert ident.username == "a"
        assert ident.display_name == "A"

    def test_user_fields(self):
        ident = PlatformIdentity(
            platform=Platform.DISCORD, user_id="1", username="a", display_name="A"
        )
        user = User(
            username="a",
            identities=[ident],
            meta={},
            created_at=123.0,
            permission_overrides={"root": True},
        )
        assert user.username == "a"
        assert user.identities == [ident]
        assert user.created_at == 123.0
        assert user.permission_overrides == {"root": True}

    def test_user_permission_overrides_default_empty(self):
        ident = PlatformIdentity(
            platform=Platform.DISCORD, user_id="1", username="a", display_name="A"
        )
        user = User(username="a", identities=[ident], meta={}, created_at=123.0)
        assert user.permission_overrides == {}

    def test_user_has_no_permission_template_field(self):
        """Named tiers are gone — User no longer stores a template name."""
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(User)}
        assert "permission_template" not in field_names


# ---------------------------------------------------------------------------
# Legacy migration guard (docs/PERMISSIONS-PLAN.md §2's migration note)
#
# Two historical shapes an existing users.db can be in, checked in the order
# this codebase actually produced them:
#   1. Oldest: only permission_level INTEGER (pre-dates templates entirely).
#   2. Short-lived middle state: permission_template TEXT +
#      permission_overrides TEXT (the multi-template system this file
#      briefly implemented before being simplified to one global template).
# Both must land on permission_overrides holding a FULL explicit dict (every
# Permission name present) so a migrated user's effective permissions don't
# shift no matter what permissions.template ends up being configured to.
# ---------------------------------------------------------------------------

class TestTemplateNameForLevel:
    @pytest.mark.parametrize("level,expected_template", [
        (0, "guest"),
        (10, "guest"),
        (25, "member"),
        (49, "member"),
        (50, "trusted"),
        (89, "trusted"),
        (90, "operator"),
        (100, "operator"),
    ])
    def test_template_name_for_level_backfill_ranges(self, level, expected_template):
        assert _template_name_for_level(level) == expected_template


class TestExplicitOverridesFor:
    def test_every_permission_present(self):
        overrides = _explicit_overrides_for(frozenset({Permission.FILE_READ}))
        assert set(overrides) == {p.value for p in Permission}

    def test_values_match_membership(self):
        overrides = _explicit_overrides_for(frozenset({Permission.FILE_READ, Permission.ROOT}))
        assert overrides[Permission.FILE_READ.value] is True
        assert overrides[Permission.ROOT.value] is True
        assert overrides[Permission.NETWORK_WRITE.value] is False

    def test_empty_set_yields_all_false(self):
        overrides = _explicit_overrides_for(frozenset())
        assert all(v is False for v in overrides.values())


class TestLegacyMigrationFromPermissionLevel:
    """Oldest shape: only permission_level INTEGER, no permission_template
    or permission_overrides columns at all."""

    def _make_legacy_db(self, tmp_path, rows: list[tuple[str, int]]) -> None:
        db_path = tmp_path / "users.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE users (
                username          TEXT PRIMARY KEY,
                permission_level  INTEGER NOT NULL,
                identities        TEXT NOT NULL DEFAULT '[]',
                meta              TEXT NOT NULL DEFAULT '{}',
                created_at        REAL NOT NULL
            );
            CREATE TABLE user_platform_index (
                platform TEXT NOT NULL,
                user_id  TEXT NOT NULL,
                username TEXT NOT NULL REFERENCES users(username),
                PRIMARY KEY (platform, user_id)
            );
        """)
        for username, level in rows:
            conn.execute(
                "INSERT INTO users (username, permission_level, identities, meta, created_at) "
                "VALUES (?, ?, '[]', '{}', 0.0)",
                (username, level),
            )
        conn.commit()
        conn.close()

    def test_legacy_level_frozen_into_full_explicit_overrides(self, tmp_path):
        self._make_legacy_db(tmp_path, [("alice", 50), ("bob", 0), ("carol", 100)])

        store = UserStore(data_dir=tmp_path)

        alice_expected = _explicit_overrides_for(_LEGACY_TEMPLATES["trusted"])
        bob_expected   = _explicit_overrides_for(_LEGACY_TEMPLATES["guest"])
        carol_expected = _explicit_overrides_for(_LEGACY_TEMPLATES["operator"])

        assert store.get_user("alice").permission_overrides == alice_expected
        assert store.get_user("bob").permission_overrides == bob_expected
        assert store.get_user("carol").permission_overrides == carol_expected

    def test_effective_permissions_unaffected_by_new_global_template(self, tmp_path):
        """A migrated user's behaviour must not shift just because the
        (now single) global template is configured differently than
        whatever they used to resolve against."""
        self._make_legacy_db(tmp_path, [("alice", 50)])  # -> "trusted"
        store = UserStore(data_dir=tmp_path)
        alice = store.get_user("alice")

        weird_cfg = PermissionsConfig(template=frozenset({Permission.ROOT}))
        assert alice.effective_permissions(weird_cfg) == _LEGACY_TEMPLATES["trusted"]

    def test_permission_level_column_dropped_or_left_harmless(self, tmp_path):
        self._make_legacy_db(tmp_path, [("alice", 50)])
        UserStore(data_dir=tmp_path)

        conn = sqlite3.connect(str(tmp_path / "users.db"))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        conn.close()
        assert "permission_overrides" in cols
        assert "permission_template" not in cols  # never introduced by this path

    def test_reopen_after_migration_is_a_no_op(self, tmp_path):
        """A second UserStore() over an already-migrated db must not choke
        on missing permission_level (already dropped) or re-backfill."""
        self._make_legacy_db(tmp_path, [("alice", 25)])
        UserStore(data_dir=tmp_path)  # migrates
        store2 = UserStore(data_dir=tmp_path)  # reopen — must not raise
        assert store2.get_user("alice").permission_overrides == _explicit_overrides_for(
            _LEGACY_TEMPLATES["member"]
        )


class TestLegacyMigrationFromTemplateColumn:
    """Short-lived middle shape: permission_template TEXT +
    permission_overrides TEXT (the multi-template system, since
    simplified away)."""

    def _make_middle_db(self, tmp_path, rows: list[tuple[str, str, dict]]) -> None:
        db_path = tmp_path / "users.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE users (
                username             TEXT PRIMARY KEY,
                permission_template  TEXT NOT NULL DEFAULT '',
                permission_overrides TEXT NOT NULL DEFAULT '{}',
                identities           TEXT NOT NULL DEFAULT '[]',
                meta                 TEXT NOT NULL DEFAULT '{}',
                created_at           REAL NOT NULL
            );
            CREATE TABLE user_platform_index (
                platform TEXT NOT NULL,
                user_id  TEXT NOT NULL,
                username TEXT NOT NULL REFERENCES users(username),
                PRIMARY KEY (platform, user_id)
            );
        """)
        for username, template, overrides in rows:
            conn.execute(
                "INSERT INTO users (username, permission_template, permission_overrides, "
                "identities, meta, created_at) VALUES (?, ?, ?, '[]', '{}', 0.0)",
                (username, template, json.dumps(overrides)),
            )
        conn.commit()
        conn.close()

    def test_template_and_overrides_merged_into_full_explicit_overrides(self, tmp_path):
        self._make_middle_db(tmp_path, [
            ("alice", "member", {"file_write": True}),   # member + an extra grant
            ("bob",   "trusted", {"model_swap": False}),  # trusted minus one bool
        ])

        store = UserStore(data_dir=tmp_path)

        alice_resolved = set(_LEGACY_TEMPLATES["member"]) | {Permission.FILE_WRITE}
        bob_resolved = set(_LEGACY_TEMPLATES["trusted"]) - {Permission.MODEL_SWAP}

        assert store.get_user("alice").permission_overrides == _explicit_overrides_for(frozenset(alice_resolved))
        assert store.get_user("bob").permission_overrides == _explicit_overrides_for(frozenset(bob_resolved))

    def test_effective_permissions_unaffected_by_new_global_template(self, tmp_path):
        self._make_middle_db(tmp_path, [("alice", "trusted", {})])
        store = UserStore(data_dir=tmp_path)
        alice = store.get_user("alice")

        weird_cfg = PermissionsConfig(template=frozenset({Permission.ROOT}))
        assert alice.effective_permissions(weird_cfg) == _LEGACY_TEMPLATES["trusted"]

    def test_permission_template_column_dropped_or_left_harmless(self, tmp_path):
        self._make_middle_db(tmp_path, [("alice", "guest", {})])
        UserStore(data_dir=tmp_path)

        conn = sqlite3.connect(str(tmp_path / "users.db"))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        conn.close()
        assert "permission_overrides" in cols
        # If SQLite couldn't DROP COLUMN, the stale column is left in place
        # but harmless (nothing reads it) — either outcome is acceptable.

    def test_empty_template_name_falls_back_to_guest(self, tmp_path):
        self._make_middle_db(tmp_path, [("alice", "", {"root": True})])
        store = UserStore(data_dir=tmp_path)
        expected = _explicit_overrides_for(frozenset({Permission.ROOT}))
        assert store.get_user("alice").permission_overrides == expected

    def test_reopen_after_migration_is_a_no_op(self, tmp_path):
        self._make_middle_db(tmp_path, [("alice", "operator", {})])
        UserStore(data_dir=tmp_path)  # migrates
        store2 = UserStore(data_dir=tmp_path)  # reopen — must not raise
        assert store2.get_user("alice").permission_overrides == _explicit_overrides_for(
            _LEGACY_TEMPLATES["operator"]
        )


# ---------------------------------------------------------------------------
# permission_overrides: corrupt-column tolerance (_parse_overrides) and
# unknown-key dropping (User.effective_permissions)
# ---------------------------------------------------------------------------

class TestOverridesRobustness:
    def test_corrupt_overrides_json_is_ignored_not_fatal(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        store._conn.execute(
            "UPDATE users SET permission_overrides = ? WHERE username = ?",
            ("not valid json {{{", user.username),
        )
        store._conn.commit()

        # Force a fresh read past the in-memory cache (the direct SQL write
        # above bypassed update_user(), so the cache is now stale).
        store._cache_by_username.pop(user.username, None)
        fetched = store.get_user(user.username)
        assert fetched.permission_overrides == {}

    def test_non_dict_overrides_json_is_ignored(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        store._conn.execute(
            "UPDATE users SET permission_overrides = ? WHERE username = ?",
            (json.dumps([1, 2, 3]), user.username),
        )
        store._conn.commit()
        store._cache_by_username.pop(user.username, None)

        fetched = store.get_user(user.username)
        assert fetched.permission_overrides == {}

    def test_unknown_permission_key_dropped_from_effective_permissions(self, store):
        """A permission_overrides entry naming a Permission that no longer
        exists (renamed/removed) must not make the user unloadable, and
        must simply be dropped rather than crashing effective_permissions()."""
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        user.permission_overrides = {
            "file_read": True,
            "some_retired_permission_name": True,
        }
        store.update_user(user)
        fetched = store.get_user(user.username)

        cfg = PermissionsConfig()
        effective = fetched.effective_permissions(cfg)
        assert Permission.FILE_READ in effective
        # The unknown key must not have made it into a real Permission —
        # it's simply absent, not silently coerced into something else.
        assert all(isinstance(p, Permission) for p in effective)


# ---------------------------------------------------------------------------
# Single global template — a fresh user resolves directly against
# PermissionsConfig.template, nothing to look up by name.
# ---------------------------------------------------------------------------

class TestSingleGlobalTemplate:
    def test_fresh_user_resolves_directly_against_global_template(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        cfg = PermissionsConfig(template=frozenset({Permission.FILE_READ, Permission.MEMORY_READ}))
        assert user.effective_permissions(cfg) == frozenset({Permission.FILE_READ, Permission.MEMORY_READ})

    def test_default_config_template_is_empty(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        assert user.effective_permissions(PermissionsConfig()) == frozenset()

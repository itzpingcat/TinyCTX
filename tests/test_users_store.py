"""
tests/test_users_store.py

Tests for users/store.py (UserStore) and users/models.py (User, PlatformIdentity).
Covers user creation, resolve_user idempotency, username generation/uniqueness
(_slugify, _random_username), rename/merge conflict behavior, and
persistence across store reopen with the same data_dir.

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.contracts import Platform
from TinyCTX.users.models import PlatformIdentity, User
from TinyCTX.users.store import (
    UserStore,
    UsernameConflictError,
    _random_username,
    _slugify,
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
        assert user.permission_level == 25
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

    def test_update_user_persists_permission_level(self, store):
        user = store.resolve_user(Platform.DISCORD, "u1", "alice", "Alice")
        user.permission_level = 90
        store.update_user(user)
        fetched = store.get_user(user.username)
        assert fetched.permission_level == 90


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
            permission_level=25,
            identities=[ident],
            meta={},
            created_at=123.0,
        )
        assert user.username == "a"
        assert user.identities == [ident]
        assert user.created_at == 123.0

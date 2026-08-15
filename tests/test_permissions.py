"""
tests/test_permissions.py

Tests for TinyCTX/permissions.py directly: the Permission enum's shape,
expand()'s implication table, and — the sharpest requirement in the plan —
the "expand the REQUIREMENT, never the GRANT" assertion from
docs/PERMISSIONS-PLAN.md §1.2. Also covers User.effective_permissions()'s
merge logic (template | overrides, override wins either direction), since
that's the other half of §1.2/§2 that isn't exercised anywhere else as a
focused, isolated unit test (test_users_store.py covers it incidentally
through UserStore persistence; this file tests the merge logic itself,
independent of storage).

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.permissions import ALL_PERMISSIONS, Permission, expand
from TinyCTX.config import PermissionsConfig
from TinyCTX.users.models import User


# ---------------------------------------------------------------------------
# Permission enum shape
# ---------------------------------------------------------------------------

class TestPermissionEnum:
    def test_seventeen_named_permissions(self):
        assert len(ALL_PERMISSIONS) == 17

    def test_all_permissions_are_str_enum_members(self):
        for p in ALL_PERMISSIONS:
            assert isinstance(p, Permission)
            assert isinstance(p.value, str)

    def test_values_are_unique(self):
        values = [p.value for p in Permission]
        assert len(values) == len(set(values))

    def test_root_is_a_member(self):
        assert Permission.ROOT in ALL_PERMISSIONS

    def test_all_permissions_matches_full_enum(self):
        assert ALL_PERMISSIONS == frozenset(Permission)


# ---------------------------------------------------------------------------
# expand() — implication table
# ---------------------------------------------------------------------------

class TestExpand:
    def test_network_write_implies_network_read(self):
        out = expand({Permission.NETWORK_WRITE})
        assert Permission.NETWORK_READ in out
        assert Permission.NETWORK_WRITE in out

    def test_network_read_does_not_imply_network_write(self):
        out = expand({Permission.NETWORK_READ})
        assert Permission.NETWORK_WRITE not in out
        assert out == frozenset({Permission.NETWORK_READ})

    def test_file_write_does_not_imply_file_read(self):
        """Deliberately asymmetric per the module docstring: rm/write_file
        don't read, so FILE_WRITE must not pull in FILE_READ."""
        out = expand({Permission.FILE_WRITE})
        assert Permission.FILE_READ not in out
        assert out == frozenset({Permission.FILE_WRITE})

    def test_file_read_does_not_imply_file_write(self):
        out = expand({Permission.FILE_READ})
        assert out == frozenset({Permission.FILE_READ})

    def test_root_implies_nothing_extra(self):
        """ROOT is deliberately NOT wired into the implication table — it is
        a distinct catch-all capability, not the top of a lattice."""
        out = expand({Permission.ROOT})
        assert out == frozenset({Permission.ROOT})

    def test_permission_with_no_implications_passes_through_unchanged(self):
        out = expand({Permission.MEMORY_READ})
        assert out == frozenset({Permission.MEMORY_READ})

    def test_empty_input_returns_empty(self):
        assert expand(set()) == frozenset()

    def test_expand_is_additive_across_multiple_inputs(self):
        out = expand({Permission.NETWORK_WRITE, Permission.MEMORY_WRITE})
        assert out == frozenset({
            Permission.NETWORK_WRITE, Permission.NETWORK_READ, Permission.MEMORY_WRITE,
        })

    def test_expand_accepts_any_iterable_not_just_sets(self):
        out = expand([Permission.NETWORK_WRITE])
        assert Permission.NETWORK_READ in out

    def test_expand_returns_frozenset(self):
        assert isinstance(expand({Permission.FILE_READ}), frozenset)

    def test_idempotent(self):
        once = expand({Permission.NETWORK_WRITE})
        twice = expand(once)
        assert once == twice


# ---------------------------------------------------------------------------
# §1.2's central assertion: expansion applies to the REQUIREMENT, never the
# GRANT. A user with NETWORK_WRITE=true and an explicit NETWORK_READ=false
# override must NOT have NETWORK_READ silently restored by expand() being
# (mis-)applied to their effective_permissions() — the more specific
# statement of intent (the explicit denial) must win.
# ---------------------------------------------------------------------------

class TestRequirementNotGrantExpansion:
    def test_expand_must_not_be_applied_to_a_grant_set(self):
        """Directly demonstrates why expand() must only ever be called on
        `needed`: naively expanding a user's raw template grant would
        fabricate a NETWORK_READ the user's override explicitly revoked."""
        cfg = PermissionsConfig(
            default_template="custom",
            templates={"custom": frozenset({Permission.NETWORK_WRITE})},
        )
        user = User(
            username="u1", identities=[], meta={}, created_at=0.0,
            permission_template="custom",
            permission_overrides={Permission.NETWORK_READ.value: False},
        )
        effective = user.effective_permissions(cfg)
        # The override is honored: NETWORK_READ is explicitly denied even
        # though the template alone would (via NETWORK_WRITE) seem to imply
        # it — because effective_permissions() never calls expand() on the
        # grant side, only the plain template | override merge.
        assert Permission.NETWORK_WRITE in effective
        assert Permission.NETWORK_READ not in effective

        # Meanwhile a REQUIREMENT of {NETWORK_READ} against this same user
        # correctly denies them, since the requirement side legitimately
        # expands NETWORK_WRITE -> {NETWORK_WRITE, NETWORK_READ} only when
        # checking what a tool NEEDS, not when computing what the user HAS.
        needed = expand({Permission.NETWORK_READ})
        missing = needed - effective
        assert missing == {Permission.NETWORK_READ}

    def test_requirement_of_network_write_is_not_satisfied_by_write_alone_if_read_denied(self):
        """A tool requiring NETWORK_WRITE expands to needing NETWORK_READ too
        (§1.2) — so a user who holds NETWORK_WRITE but has explicitly denied
        NETWORK_READ via override must still fail that check."""
        cfg = PermissionsConfig(
            default_template="custom",
            templates={"custom": frozenset({Permission.NETWORK_WRITE, Permission.NETWORK_READ})},
        )
        user = User(
            username="u2", identities=[], meta={}, created_at=0.0,
            permission_template="custom",
            permission_overrides={Permission.NETWORK_READ.value: False},
        )
        effective = user.effective_permissions(cfg)
        assert Permission.NETWORK_READ not in effective

        needed = expand({Permission.NETWORK_WRITE})
        missing = needed - effective
        assert Permission.NETWORK_READ in missing


# ---------------------------------------------------------------------------
# User.effective_permissions() — template | overrides merge, override wins
# ---------------------------------------------------------------------------

class TestEffectivePermissionsMerge:
    def _cfg(self):
        return PermissionsConfig(
            default_template="guest",
            templates={
                "guest": frozenset(),
                "member": frozenset({Permission.FILE_READ, Permission.NETWORK_READ}),
            },
        )

    def test_plain_template_no_overrides(self):
        user = User(username="u", identities=[], meta={}, created_at=0.0, permission_template="member")
        assert user.effective_permissions(self._cfg()) == frozenset({Permission.FILE_READ, Permission.NETWORK_READ})

    def test_override_true_grants_bool_template_lacked(self):
        user = User(
            username="u", identities=[], meta={}, created_at=0.0,
            permission_template="guest",
            permission_overrides={Permission.FILE_READ.value: True},
        )
        assert Permission.FILE_READ in user.effective_permissions(self._cfg())

    def test_override_false_revokes_bool_template_granted(self):
        user = User(
            username="u", identities=[], meta={}, created_at=0.0,
            permission_template="member",
            permission_overrides={Permission.FILE_READ.value: False},
        )
        effective = user.effective_permissions(self._cfg())
        assert Permission.FILE_READ not in effective
        assert Permission.NETWORK_READ in effective  # untouched by the override

    def test_empty_template_falls_back_to_default(self):
        user = User(username="u", identities=[], meta={}, created_at=0.0, permission_template="")
        assert user.effective_permissions(self._cfg()) == frozenset()  # guest

    def test_unknown_template_falls_back_to_default_with_warning(self, caplog):
        import logging
        user = User(username="u", identities=[], meta={}, created_at=0.0, permission_template="ghost")
        with caplog.at_level(logging.WARNING):
            effective = user.effective_permissions(self._cfg())
        assert effective == frozenset()  # falls back to guest (default_template)
        assert any("unknown template" in rec.message for rec in caplog.records)

    def test_unknown_override_key_dropped_with_warning(self, caplog):
        import logging
        user = User(
            username="u", identities=[], meta={}, created_at=0.0,
            permission_template="member",
            permission_overrides={"some_retired_permission": True},
        )
        with caplog.at_level(logging.WARNING):
            effective = user.effective_permissions(self._cfg())
        # Doesn't raise, doesn't add anything real, member's grants intact.
        assert effective == frozenset({Permission.FILE_READ, Permission.NETWORK_READ})
        assert any("unknown permission override" in rec.message for rec in caplog.records)

    def test_has_permission_wraps_effective_permissions(self):
        user = User(username="u", identities=[], meta={}, created_at=0.0, permission_template="member")
        cfg = self._cfg()
        assert user.has_permission(Permission.FILE_READ, cfg) is True
        assert user.has_permission(Permission.FILE_WRITE, cfg) is False


# ---------------------------------------------------------------------------
# PermissionsConfig.resolve_template()
# ---------------------------------------------------------------------------

class TestResolveTemplate:
    def test_known_template_resolves(self):
        cfg = PermissionsConfig(templates={"x": frozenset({Permission.ROOT})})
        assert cfg.resolve_template("x") == frozenset({Permission.ROOT})

    def test_unknown_template_falls_back_to_default(self, caplog):
        import logging
        cfg = PermissionsConfig(default_template="guest", templates={"guest": frozenset()})
        with caplog.at_level(logging.WARNING):
            result = cfg.resolve_template("nonexistent")
        assert result == frozenset()

    def test_empty_name_uses_default_silently(self, caplog):
        import logging
        cfg = PermissionsConfig(default_template="guest", templates={"guest": frozenset({Permission.FILE_READ})})
        with caplog.at_level(logging.WARNING):
            result = cfg.resolve_template("")
        assert result == frozenset({Permission.FILE_READ})
        assert not any("unknown template" in rec.message for rec in caplog.records)

    def test_default_builtin_templates_shape(self):
        cfg = PermissionsConfig()
        assert cfg.templates["guest"] == frozenset()
        assert cfg.templates["operator"] == frozenset(Permission)
        assert Permission.UNTRUSTED_EXEC not in cfg.templates["trusted"]
        assert Permission.FILE_READ in cfg.templates["member"]
        assert Permission.FILE_WRITE not in cfg.templates["member"]

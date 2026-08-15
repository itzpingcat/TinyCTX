from __future__ import annotations

import logging
from dataclasses import dataclass, field

from TinyCTX.contracts import Platform
from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)


@dataclass
class PlatformIdentity:
    platform:     Platform
    user_id:      str   # platform-native ID (e.g. Discord snowflake)
    username:     str   # platform handle / login name
    display_name: str   # human-readable display name


@dataclass
class User:
    username:               str                    # TinyCTX username — primary key, globally unique
    identities:              list[PlatformIdentity]  # all known platform accounts for this human
    meta:                     dict                    # freeform per-user data for modules
    created_at:               float                   # unix timestamp
    # Sparse diff against the single global permissions.template (config.yaml)
    # — only entries that differ from it are stored here. {permission_value:
    # bool}, keyed by Permission.value (a plain str, since this round-trips
    # through JSON in users.db). Override wins over the template either
    # direction: True grants a bool the template didn't have, False revokes
    # one it did. See docs/PERMISSIONS-PLAN.md §2 and TinyCTX/permissions.py.
    permission_overrides:     dict[str, bool] = field(default_factory=dict)

    def effective_permissions(self, permissions_config) -> frozenset[Permission]:
        """
        Resolve this user's actual granted permission set:
        permissions_config.template | permission_overrides, override wins.
        `permissions_config` is a TinyCTX.config.PermissionsConfig (passed
        in rather than imported, since users/models.py must not import
        config — see users/store.py's module docstring for the layering
        this avoids).

        Unknown keys in permission_overrides (a permission since renamed or
        removed) are dropped with a warning rather than raising — a stale
        override must not make a user unloadable. See
        docs/PERMISSIONS-PLAN.md §2's robustness requirements.
        """
        granted = set(permissions_config.template)
        for name, value in self.permission_overrides.items():
            try:
                perm = Permission(name)
            except ValueError:
                logger.warning(
                    "users: unknown permission override %r on user %r dropped",
                    name, self.username,
                )
                continue
            if value:
                granted.add(perm)
            else:
                granted.discard(perm)
        return frozenset(granted)

    def has_permission(self, perm: Permission, permissions_config) -> bool:
        """Convenience wrapper — `perm in user.effective_permissions(cfg)`."""
        return perm in self.effective_permissions(permissions_config)

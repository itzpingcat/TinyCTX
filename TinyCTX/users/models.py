from __future__ import annotations

from dataclasses import dataclass, field

from TinyCTX.contracts import Platform


@dataclass
class PlatformIdentity:
    platform:     Platform
    user_id:      str   # platform-native ID (e.g. Discord snowflake)
    username:     str   # platform handle / login name
    display_name: str   # human-readable display name


@dataclass
class User:
    username:         str                      # TinyCTX username — primary key, globally unique
    permission_level: int                      # 0-100
    identities:       list[PlatformIdentity]   # all known platform accounts for this human
    meta:             dict                     # freeform per-user data for modules
    created_at:       float                    # unix timestamp

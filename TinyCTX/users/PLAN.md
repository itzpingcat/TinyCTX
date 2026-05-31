# users/ — implementation plan

Stable persistent user identity for TinyCTX.
Replaces the fragile plaintext `UserIdentity` strings currently produced by bridges.

---

## motivation

Current state:
- Bridges construct a `UserIdentity(platform, user_id, username)` inline on every message.
- Nothing is persisted — identity has no memory between sessions.
- No way to bind per-user data (memory, permissions, preferences) to a stable key.
- Same human on Discord and Matrix is invisible to the system as one person.

Goals:
- One persistent `User` object per human, keyed by a unique TinyCTX username.
- Bridges call `resolve_user(...)` and get back a `User` — creation/lookup is automatic.
- Cross-platform identity: a single `User` carries a list of all known platform accounts.
- Merge and rename utilities for manual identity management.

---

## file layout

```
TinyCTX/users/
    __init__.py     # public API surface
    models.py       # User, PlatformIdentity dataclasses
    store.py        # UserStore — SQLite + LRU cache + all business logic
    PLAN.md         # this file
```

### users.db location — security rationale

`users.db` must be:
- Writable at runtime (new users are created on first message)
- NOT accessible to the agent (privilege escalation: agent instructed to bump its
  own user's permission_level via a filesystem or shell tool)

This rules out:
- The workspace — agent has filesystem tool access there
- The package directory — code is deployed read-only

Resolution: a config directory outside the workspace, writable by the `tinyctx`
process, never passed to any agent tool.

`UserStore` resolves the path via the `TINYCTX_CONFIG_DIR` environment variable.
If unset, it falls back to `platformdirs.user_config_dir("tinyctx")`:

```
Container:  /etc/tinyctx/users.db      (TINYCTX_CONFIG_DIR=/etc/tinyctx)
Linux/Mac:  ~/.config/tinyctx/users.db (platformdirs fallback)
Windows:    %APPDATA%\tinyctx\users.db  (platformdirs fallback)
```

The agent's filesystem tools are scoped to `TINYCTX_WORKSPACE_PATH`. The config dir
is outside that path and never passed to any tool, so it is structurally unreachable
regardless of what the agent is told to do.

---

## phase 3 note — config file location

In phase 3, `config.yaml` itself will move to the same config directory:

```
~/.config/tinyctx/config.yaml   (Linux/Mac)
%APPDATA%\tinyctx\config.yaml   (Windows)
```

The TinyCTX package directory will contain a `pointer.txt` file with the absolute
path to the config file. On startup, `main.py` reads `pointer.txt` to locate
`config.yaml` rather than assuming a fixed relative path. This makes the package
directory fully read-only at runtime — all mutable state lives in the config dir
or the workspace.

`users.db` is already in the config dir, so no migration needed for it at phase 3.

---

## data model

### `models.py`

```python
@dataclass
class PlatformIdentity:
    platform:     Platform   # Platform enum from contracts.py
    user_id:      str        # platform-native ID (e.g. Discord snowflake)
    username:     str        # platform handle / login name
    display_name: str        # human-readable display name

@dataclass
class User:
    username:         str                    # TinyCTX username — primary key, globally unique
    permission_level: int                    # 0-100
    identities:       list[PlatformIdentity] # all known platform accounts for this human
    meta:             dict                   # freeform per-user data for modules
    created_at:       float                  # unix timestamp
```

`User.username` is the stable internal key used everywhere in TinyCTX.
`PlatformIdentity` entries are the raw platform-side facts; bridges supply these.
Permissions are on `User` only — `InboundMessage` does not carry a permission level.
Permission system rework (bridge slash commands, etc.) is deferred to phase 2.

---

## database schema

```sql
CREATE TABLE users (
    username         TEXT PRIMARY KEY,
    permission_level INTEGER NOT NULL DEFAULT 25,
    identities       TEXT NOT NULL DEFAULT '[]',  -- JSON list of PlatformIdentity dicts
    meta             TEXT NOT NULL DEFAULT '{}',  -- JSON object
    created_at       REAL NOT NULL
);

-- Write-through lookup index: (platform, platform_user_id) → TinyCTX username.
-- Avoids full-table JSON scans on every message.
CREATE TABLE user_platform_index (
    platform TEXT NOT NULL,
    user_id  TEXT NOT NULL,   -- platform-native ID
    username TEXT NOT NULL REFERENCES users(username),
    PRIMARY KEY (platform, user_id)
);
```

`user_platform_index` is a pure derived index — no source-of-truth data.
On any write that touches identities, both tables are updated in the same transaction.

---

## nodes store TinyCTX usernames, not platform IDs

`db.py` nodes currently store `author_id` (raw platform user ID) and `author_name`
(display name string). These will change:

- `author_id` → stores `User.username` (the TinyCTX username)
- `author_name` → removed. The agent sees only the username; display names are
  platform detail that lives in `PlatformIdentity`, not in conversation history.

`ConversationDB.add_node(author_id=user.username)` is the updated call.
The `author_name` column can be kept in the schema for now and simply left NULL
on new nodes — full column removal is a future cleanup.

Historical nodes (pre-users system) keep their raw platform IDs in `author_id` — no
migration. New nodes get the TinyCTX username.

---

## `store.py` — UserStore

Single class. Owns the SQLite connection and an LRU cache keyed on `(platform, user_id)`
and on `username`.

### public methods

```python
class UserStore:
    def __init__(self) -> None:
        """
        Resolves db path via platformdirs.user_config_dir("tinyctx").
        Creates the directory and database if they don't exist.
        Not configurable — see security rationale above.
        """

    def resolve_user(
        self,
        platform: Platform,
        user_id: str,
        username: str,
        display_name: str,
    ) -> User:
        """
        Hot path — called by every bridge on every inbound message.

        1. Hit LRU cache for (platform, user_id).
        2. If miss, look up user_platform_index in SQLite.
        3. If found: load User. If the platform-specific username or display_name
           differ from what's stored in the matching PlatformIdentity, update that
           identity entry and persist (user renamed their Discord account, etc).
           Return the (possibly updated) User.
        4. If not found: pick a TinyCTX username (see _pick_username), create new User
           row, write index entry, populate cache, return.
        """

    def get_user(self, username: str) -> User | None:
        """Fetch by TinyCTX username. Returns None if not found."""

    def update_user(self, user: User) -> None:
        """Persist mutations to permission_level, meta, or identities."""

    def merge_users(self, primary_username: str, secondary_username: str) -> User:
        """
        Fold all PlatformIdentity entries from secondary into primary.
        Update user_platform_index rows that pointed at secondary to point at primary.
        Delete secondary user row.
        All in one transaction.
        Returns the merged primary User.
        Raises ValueError if either username does not exist.
        """

    def rename_user(self, username: str, new_username: str) -> User:
        """
        Change a user's TinyCTX username.
        Updates users PK and user_platform_index.username in one transaction
        (explicit updates — SQLite does not cascade TEXT PKs by default).
        Raises UsernameConflictError if new_username is already taken.
        Returns the updated User.
        """
```

### `_pick_username` logic

```python
def _pick_username(self, platform_username: str, display_name: str) -> str:
    for candidate in [_slugify(platform_username), _slugify(display_name)]:
        if candidate and not self._username_taken(candidate):
            return candidate
    # Both taken or produced empty strings — random fallback
    while True:
        candidate = _random_username()   # e.g. "amber-fox-3817"
        if not self._username_taken(candidate):
            return candidate
```

`_slugify`: lowercase, keep alphanumeric + hyphens + underscores, strip the rest,
truncate to 32 chars. Returns empty string if result is blank.

`_random_username`: `adjective-noun-NNNN` from a small bundled wordlist — URL-safe
and recognisable enough to quote in conversation ("your TinyCTX username is
amber-fox-3817").

### caching

`dict[tuple[str, str], User]` keyed on `(platform.value, user_id)` plus a
`dict[str, User]` keyed on `username`. Both invalidated on `update_user`,
`merge_users`, and `rename_user`. No external cache library needed at this scale.

---

## `__init__.py` — public API

```python
from .models import User, PlatformIdentity
from .store import UserStore, UsernameConflictError

__all__ = ["User", "PlatformIdentity", "UserStore", "UsernameConflictError"]
```

`Runtime` instantiates one `UserStore` and exposes it as `runtime.users`.
Bridges call `runtime.users.resolve_user(...)`.

---

## changes to existing files

### `contracts.py`

- Remove `UserIdentity` dataclass (or keep as a deprecated alias during transition).
- `InboundMessage.author` changes type from `UserIdentity` to `User`.
- Remove `InboundMessage.permission_level` — permissions live on `User` only.
- Add TODO note that `contracts.py` will be split up in a future refactor.

### `runtime.py`

```python
from TinyCTX.users import UserStore

class Runtime:
    def __init__(self, config: Config) -> None:
        ...
        self.users = UserStore()  # resolves its own path via platformdirs
```

Also update `_compute_state_delta` — remove `permission_level` from the state delta
mapping (no longer on `InboundMessage`). Permission handling is phase 2.

### bridges (Discord, Matrix, CLI)

Replace inline `UserIdentity(...)` construction:

```python
# before
author = UserIdentity(platform=Platform.DISCORD, user_id=str(msg.author.id), username=msg.author.name)

# after
author = self._runtime.users.resolve_user(
    platform=Platform.DISCORD,
    user_id=str(message.author.id),
    username=message.author.name,
    display_name=message.author.display_name,
)
```

Remove all `permission_level=...` arguments from `InboundMessage(...)` construction.
Remove `_resolve_permission_level` from Discord bridge — phase 2 will replace it with
a slash command that calls `UserStore.update_user()` to set `User.permission_level`.

### `db.py` / `runtime.py` node writes

```python
add_node(author_id=msg.author.username)
```

The TinyCTX username goes into `author_id`. `author_name` is not populated —
the agent sees only the username. Display name detail lives in `PlatformIdentity`.

---

## implementation order

1. `users/models.py` — dataclasses, no logic
2. `users/store.py` — UserStore with schema, resolve_user, helpers
3. `users/__init__.py` — re-exports
4. `contracts.py` — swap UserIdentity → User, drop permission_level from InboundMessage
5. `runtime.py` — add UserStore(), update _compute_state_delta
6. Discord bridge — swap UserIdentity, remove permission_level wiring
7. Matrix bridge — same
8. CLI bridge — same
9. Grep remaining `UserIdentity` / `permission_level` references and clean up

---

## non-goals (for now)

- No automatic cross-platform linking (linking is manual via `merge_users`).
- No admin commands wired to `merge_users` / `rename_user` — CommandRegistry entries,
  phase 2.
- No migration of existing `author_id` strings in `agent.db` — historical nodes keep
  their raw platform IDs; only new messages get `User.username`.
- Permission enforcement logic is phase 2 (bridge slash commands → UserStore.update_user).
- Phase 4: knowledge module uses `User` to curate per-user context (memory graph
  keyed on `User.username` rather than raw platform ID, shared across linked identities).

---

## docker deployment note

The container runs as the `tinyctx` user (`HOME=/home/tinyctx`). The workspace is
already mounted at `/home/tinyctx` — so the config dir must live outside it.

### Dockerfile addition

```dockerfile
RUN mkdir -p /etc/tinyctx && chown tinyctx:tinyctx /etc/tinyctx
```

This gives the `tinyctx` user write access to `/etc/tinyctx` without touching the
workspace mount.

### compose.yaml additions

```yaml
volumes:
  - type: bind
    source: ${TINYCTX_CONFIG:-~/.config/tinyctx}
    target: /etc/tinyctx

environment:
  TINYCTX_CONFIG_DIR: /etc/tinyctx
```

The host-side default (`~/.config/tinyctx`) matches the platformdirs fallback used
when running outside Docker, so the same host directory works in both contexts.

This covers both `users.db` now and `config.yaml` in phase 3. Without this mount,
a new container starts with no users and no config.

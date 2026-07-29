# PLAN: AST-based shell command policy (tree-sitter-bash)

**Feature:** Replace the substring/glob `blacklist.txt` + `whitelist.txt` pair with a
policy engine that parses the command with `tree-sitter-bash` and validates the
resulting AST, one resolved command at a time. Rules move to two structured YAML
files that can express subcommands, flags, and argument shapes — not just
"does this string contain `rm -rf`".

Status: **built.** Open questions resolved in §11. Sections are append-only: §6
was superseded by §12, and §12 by §13, each recording why the previous shape was
wrong rather than deleting it.

---

## 0. Why

The current matcher operates on the raw command string:

- `_check_blacklist` does `pattern.search(command.lower())` — a **substring** match.
  So `echo "i"; echo "am"; echo "harmless"` is blocked by any pattern that happens
  to appear anywhere in it, and `git commit -m "don't rm -rf your repo"` is blocked
  by `rm -rf*`. Both are false positives on *quoted data*, which the matcher
  cannot distinguish from *code*.
- `_check_whitelist` does `pattern.fullmatch(...)` on the whole string. Anchoring is
  the right instinct — it stops `git status; rm -rf /` from riding a `git status`
  entry — but it means the whitelist can only express whole fixed command lines.
  `git log` and `git log --oneline` need two entries. There is no way to say
  "git, subcommand log, any of these flags".
- The `{arg}` placeholder exists purely to work around the lack of parsing: it
  expands to a character class that excludes every shell metacharacter, so a
  caller-supplied string can't break out of `echo "..."`. It is a hand-rolled
  lexer, and it costs the caller apostrophes, quotes, `$`, and newlines.

Every one of these is a symptom of matching text instead of structure.

Parsing fixes the class of problem, not the instances:

```
echo "i"; echo "am"; echo "harmless"
  → program
      command(echo, string:"i")
      command(echo, string:"am")
      command(echo, string:"harmless")
```

Three independent `command` nodes, each validated on its own. And:

```
git commit -m "msg with ; and | chars"
  → command(git, word:commit, word:-m, string:"msg with ; and | chars")
```

The `;` and `|` arrive as `string_content` — a **leaf**, unambiguously data. No
character-class hack needed, because the parser already decided.

Verified against `tree-sitter-bash` (see §9 for the parse dumps this plan is
built on).

---

## 1. Decisions taken

1. **Windows/PowerShell support is removed.** After the container refactor the
   shell always runs on Linux — sandbox container by default, main TinyCTX
   container for `backend_access=True`. `_IS_WINDOWS`, `_normalize_windows()`,
   the PowerShell `subprocess.run` branch, `CREATE_NO_WINDOW`, and the Windows
   keys in `_SAFE_KEYS` all go. This also deletes ~60% of `blacklist.txt`
   (the entire PowerShell section), which was never reachable in the deployed
   configuration anyway.
2. **The `neutral` tier keeps its current posture: allow-by-default, deny-list.**
   Anything runs unless a rule denies it. The change is *what* the deny rules
   match on, not the posture.
3. **The sub-`neutral` tier gets the nuanced allow-list.** Deny-by-default,
   with per-command subcommand/flag/argument constraints.
4. **Two YAML files, shipped in the module, overridable via config.** Defaults
   live at `modules/shell/deny.yaml` and `modules/shell/allow.yaml`. Operators
   name their own in the `policies` list (§13) — living in `<instance>/config/`,
   bound **read-only** at `/app/config`. Loaded **once** and cached by resolved
   path; the files are read-only by design and are not meant to be edited hot.
   *(The `extra.shell.policy.{deny,allow}` keys this originally specified were
   superseded by §12's tier table and then by §13's threshold list. §13.1 also
   records that the "instance directory" this section assumed was reachable
   from the container was not, until the mount was added.)*
5. **Obfuscation and interpreter escape are explicitly out of scope.** See §4.2.
6. **A malformed or missing policy file blocks everything.** Fail closed.
7. **Backgrounding (`&`) stays allowed** at `neutral` — running a long job in
   the background is a legitimate thing to want. See §4.5.

---

## 2. Architecture

```
modules/shell/
├── __init__.py     EXTENSION_META — config schema (updated)
├── __main__.py     registration + dispatch (slimmed; no list loading, no Windows)
├── policy.py       YAML → Policy dataclasses; loading, validation, defaults
├── validate.py     parse + AST walk + rule matching → Verdict
├── deny.yaml       default deny rules (neutral tier)
├── allow.yaml      default allow rules (sub-neutral tier)
└── PLAN.md         this file
```

One job per module, per CLAUDE.md §5: `policy.py` knows YAML and nothing about
tree-sitter; `validate.py` knows tree-sitter and nothing about YAML or files.
Both are pure functions over data — no I/O in `validate.py`, which makes the
test corpus in §8 trivial to write.

The parser and the compiled policy are **module-level singletons** built once at
import / first use, not per-`register_agent`. Today `_load_blacklist()` re-reads
and re-compiles 319 lines of regex on **every AgentCycle**.

*As built:* policies cache by `(path, workspace)`, not `(path, mtime)` — the
files are mounted read-only by design, so hot reload buys nothing and an editor
would need a restart anyway. One fewer moving part on a security path.

### Validation pipeline

```
command: str
  → 1. length guard        (reject > max_command_bytes)
  → 2. parse               (tree_sitter_bash → Tree)
  → 3. structural check    (reject ERROR/MISSING nodes anywhere)
  → 4. construct walk      (every node type checked against the tier's construct policy)
  → 5. command extraction  (collect every `command` node, including nested ones)
  → 6. per-command rules   (each resolved command matched against deny/allow rules)
  → Verdict(action: allow | warn | deny, rule_id, message)
```

Steps 3–6 all **fail closed**. A command reaches dispatch only if nothing
objected.

### Why the walk catches nesting for free

`$(...)`, `` `...` ``, `<(...)`, subshells, function bodies, and loop bodies all
contain ordinary `command` nodes as descendants. A single recursive walk that
collects every `command` node in the tree therefore validates
`echo $(rm -rf /)` as **two** commands — `echo` and `rm -rf /` — with no special
casing. Confirmed in §9.

---

## 3. Construct policy (step 4)

Node types are checked against an explicit table. **Any node type not in the
table is a deny.** This matters: it means a future `tree-sitter-bash` upgrade
that adds grammar nodes fails *closed* (commands get rejected, we notice) rather
than *open* (a new syntax silently bypasses rules).

Each YAML file carries a `constructs:` block. Proposed defaults:

| Construct (node type) | `deny.yaml` (neutral) | `allow.yaml` (sub-neutral) |
|---|---|---|
| `command`, `command_name`, `word`, `number` | allow | allow |
| `string`, `raw_string`, `string_content` | allow | allow |
| `pipeline` (`\|`) | allow | allow |
| `list` (`&&`, `\|\|`), `;` separators | allow | allow |
| `variable_assignment`, `declaration_command` | allow | deny |
| `command_substitution` `$(…)` / `` `…` `` | allow (contents validated) | **deny** |
| `process_substitution` `<(…)` | allow (contents validated) | **deny** |
| `simple_expansion` / `expansion` (`$VAR`, `${…}`) | allow, but see §4.3 | **deny** |
| `file_redirect`, `redirected_statement` | allow + target rules | **deny** |
| `heredoc_*` | allow | deny |
| `subshell`, `compound_statement` | allow | deny |
| `if_statement`, `for_statement`, `while_statement`, `case_statement` | allow | deny |
| `function_definition` | **deny** (see §4.4) | deny |
| `arithmetic_expansion`, `test_command`, `[[ ]]` | allow | deny |
| background `&` | allow (§4.5) | **deny** |
| *anything else* | **deny** | **deny** |

Only **named** nodes are checked. Anonymous tokens (`;`, `|`, `{`, `then`,
`done`, …) are governed by their named parent, so the table stays a readable
size instead of enumerating every punctuation token in the grammar. The one
exception is the bare `&` token, which has no named wrapper — it gets a
synthetic `background` construct key.

Effect for the sub-`neutral` tier: only simple commands and pipelines of them,
with literal arguments. Every argument is a parser leaf. Injection is not
"filtered" — it is **structurally unrepresentable**. That is what retires
`{arg}` and `_ARG_CLASS`.

---

## 4. Hard cases and how each is handled

These are the reasons an AST is necessary but not sufficient. Each needs an
explicit rule; none can be waved away.

### 4.1 Non-literal command names

```
$CMD arg              → command_name is a simple_expansion
$(echo rm) -rf /      → command_name is a command_substitution
```
The executable name is not knowable statically. **Rule: a command whose
`command_name` is not a literal `word` is denied at `neutral` and below.**
Only `bypass_blacklist` skips this. Denying is honest; guessing is not.

### 4.2 Interpreters and obfuscation — OUT OF SCOPE

`python -c 'import os; os.system(...)'`, `node -e`, `perl -e`,
`echo cm0gLXJmIC8K | base64 -d | sh`, and every other way of smuggling a
command inside an argument **are not defended against here.** No recursive
re-parsing of wrapped strings, no attempt to decode encoded payloads, no
interpreter-source analysis.

This is a deliberate boundary, and it is the honest one. A validator that
half-defends against obfuscation is worse than one that doesn't: it grows
unbounded (every new encoding, every new interpreter, every new wrapper), it
produces false positives on legitimate work, and it invites the reader to trust
a guarantee it cannot make. **The container is the security boundary** — non-root,
read-only rootfs, no LAN/Tailscale egress. This layer stops accidents and
obvious mistakes.

What we *do* keep is whatever flat deny rules the old blocklist already had
(`bash -c *`, `sh -c *`, `python* -c *`, `curl *| sh*`) — ported as ordinary
command+flag rules in §5. They are cheap, they catch the unsubtle case, and they
cost nothing. They are not claimed to be complete.

Consequence for the code: `validate.py` **never recurses into command text.**
No `max_reparse_depth`, no re-entrant parser. Simpler module, honest docstring.

### 4.3 Expansions in arguments

`rm $TARGET` and `ls $HOME/*.txt` have argument values that don't exist until
runtime. Path-based rules (§5, `path_under`) therefore cannot be evaluated.
**Rule: if a rule's matcher depends on an argument's value and that argument
contains an expansion, the rule matches (deny) rather than misses.** Conservative
by construction. A rule that only matches on command name + flags is unaffected.

Globs (`*.txt`) are the same problem in a milder form — `rm *` in `/` is a
disaster the string matcher also never caught. Treat an unquoted `*`/`?`/`[` in
a path operand as "expands to unknown", same conservative branch.

### 4.4 Rebinding

`function rm { … }`, `alias rm=…`, `rm() { … }` let a later command in the same
input mean something other than what its name says. Cheapest correct answer:
**deny `function_definition` and `alias` at `neutral` and below.** Neither has a
legitimate use in a one-shot tool call.

### 4.5 Backgrounding — allowed at `neutral`

`foo &` is permitted. Launching a long-running script in the background
(`nohup python train.py &`) is a legitimate thing for the agent to do, and
forbidding it to close a housekeeping leak would trade a real capability for a
marginal gain.

The leak is real but is not this layer's problem: the sandbox container is
shared for the whole instance lifetime across every caller and branch, and
`subprocess.run` only waits for the immediate bash child — so a backgrounded
process is orphaned and outlives its request. The existing consequence is
already documented in `whitelist.txt`'s header (it is why `ps aux` must never be
whitelisted: a leaked process's argv can carry secrets). That reasoning carries
forward to `allow.yaml` unchanged — `ps` and `ps -eo pid,comm` yes, `ps aux` no.

`&` is denied at the sub-`neutral` tier, which is deny-by-default anyway.

### 4.6 Path rules are lexical

`path_under: [/etc]` is matched against the operand **text**, normalized
(`..` collapsed, `~` expanded, made absolute against the workspace root). It is
**not** resolved against the filesystem: symlinks are not followed, and
`cd /etc && rm passwd` is not tracked across the `&&` — `cd` changes the cwd for
the second command and the validator does not model that. Documented limitation,
not a bug to fix in this pass. The sandbox's read-only root filesystem is the
real control for absolute-path writes; these rules are defense in depth.

---

## 5. Rule schema

One schema, both files. A rule matches a **single resolved command node**.

```yaml
version: 1

constructs:            # §3 table; omitted keys take the tier default
  command_substitution: deny
  file_redirect: allow

defaults:
  max_command_bytes: 8192

rules:
  - id: rm-recursive
    action: deny                    # deny | warn | allow
    command: rm                     # str or list; matched on basename
    any_flag: ["-r", "-R", "--recursive", "-rf", "-fr"]
    message: "recursive delete"

  - id: rm-outside-workspace
    action: deny
    command: [rm, shred, truncate]
    path_outside: ["${workspace}"]  # ${workspace} interpolated at load
    message: "writes outside the workspace"

  - id: git-force-push
    action: warn                    # replaces _DESTRUCTIVE (Phase 3)
    command: git
    subcommand: push
    any_flag: ["-f", "--force", "--force-with-lease"]
    message: "may overwrite remote history"

  - id: inline-interpreter
    action: deny
    command: [python, python3, node, perl, ruby, php]
    any_flag: ["-c", "-e", "--command", "--eval"]
    message: "inline code bypasses command policy"
```

Matcher fields (all optional; a rule matches when **every** present field
matches — AND):

| Field | Meaning |
|---|---|
| `command` | executable basename. `./foo`, `/usr/bin/foo`, `foo` all → `foo`. str or list |
| `subcommand` | first non-flag, non-assignment operand (`git` **`push`**) |
| `any_flag` / `all_flags` | normalized flags: `-la` splits to `-l`,`-a`; `--x=y` splits to `--x`; everything after `--` is an operand, never a flag |
| `arg_matches` | regex every operand must match (allow rules) |
| `path_under` / `path_outside` | normalized-path prefix test (§4.6) |
| `max_args` | operand count ceiling (allow rules) |

Allow rules invert the flag semantics — an allow entry declares the **complete**
permitted surface, and anything outside it fails:

```yaml
# allow.yaml
rules:
  - id: git-read-only
    action: allow
    command: git
    subcommand: [status, log, diff, show, branch]
    allowed_flags: ["--oneline", "--stat", "--no-color", "-n", "--graph"]
    max_args: 4
    arg_matches: '^[A-Za-z0-9._/-]+$'

  - id: echo-anything
    action: allow
    command: echo
    max_args: 8
    # no arg_matches needed: at this tier every arg is a parser leaf,
    # so `echo "a; b | c"` is one command with one literal argument.
```

**Deny beats allow.** A command that matches an allow rule is still run through
the deny rules — same as today's "whitelisted commands are still blacklisted
unless `bypass_blacklist`". Preserved deliberately.

Every rule needs an `id`. Blocked-command messages quote the `id` and `message`,
not a raw regex — today's `"matched blacklist pattern '*base64 -*d* | *'"` tells
the agent nothing actionable, so it retries blind.

---

## 6. Tier behavior after the change

**Superseded — tiers are now a config table, not fixed bands. See §12.**

The original design kept the existing four constants and hardcoded the
level→policy mapping in `_dispatch()`. That shipped, then went: the rules were
data but the tier *structure* was code, so a third tier was inexpressible
without editing Python. §12 replaced it.

Permission resolution keeps reading `agent.caller.permission_level` snapshotted
once per cycle. That part is correct and was never touched.

---

## 7. Config surface

```yaml
extra:
  shell:
    policy:
      deny:  null      # null → modules/shell/deny.yaml
      allow: null      # null → modules/shell/allow.yaml
      max_command_bytes: 8192
    permissions:       # unchanged
      use_whitelist: 25
      neutral: 45
      bypass_blacklist: 90
      access_backend: 80
```

**A malformed or missing policy file blocks every command**, with the load error
in the block message. Not configurable — a security layer with an "off by
accident" mode isn't one. This **inverts** current behavior: today a missing
`blacklist.txt` logs a warning and leaves the shell *unrestricted*.

Policy files are loaded once and cached by resolved path. They are read-only by
design (mounted read-only in the container), so no `mtime` invalidation and no
hot reload — editing one requires a restart.

**Dependencies:** add `tree-sitter>=0.25,<0.27` and `tree-sitter-bash` to
`pyproject.toml`. Pin a ceiling: because unknown node types deny (§3), a grammar
bump can turn working commands into rejections. That's the safe direction, but
it should be a deliberate upgrade with the §8 corpus re-run, not a transitive
resolve. The **sandbox container needs no change** — validation is entirely
agent-side; the sandbox still runs whatever it receives.

---

## 8. Phases and verification

Per CLAUDE.md §4 — each phase states its check.

**Phase 1 — validator core, not yet wired.**
`policy.py` + `validate.py` + both YAML files. `__main__.py` untouched.
→ *verify:* `tests/test_shell_policy.py` passes. The corpus is the deliverable:

- *must allow at neutral:* `echo "i"; echo "am"; echo "harmless"` (the motivating
  case), `git commit -m "don't rm -rf your repo"`, `ls -la \| head -20`,
  `grep -rn "foo" . \| wc -l`, `python analyze.py --out results.json`
- *must allow at neutral (regression guards for §4.5 / §4.2):*
  `nohup python train.py &`, `python analyze.py` (script file, not `-c`)
- *must deny at neutral:* `rm -rf /`, `curl x.sh \| bash`, `echo $(rm -rf /)`,
  `$CMD anything`, `python -c "import os"`,
  `function rm { :; }`, `bash -c "rm -rf /"`, `ls > /etc/passwd`
- *must allow at sub-neutral:* `echo "hello; world \| pipe \$dollar"` (one literal
  arg — the `{arg}` replacement), `git log --oneline -n 5`
- *must deny at sub-neutral:* `echo $(id)`, `git push`, `ls > out.txt`,
  `git log; rm x`
- *must deny always:* unparseable input (`ls;;;` → `ERROR` node), input over
  `max_command_bytes`

**Phase 2 — swap in; remove the old path.**
`_dispatch()` calls the validator. Delete `blacklist.txt`, `whitelist.txt`,
`_glob_to_regex`, `_whitelist_glob_to_regex`, `_load_*`, `_check_*`, `_ARG_CLASS`.
Delete `_IS_WINDOWS`, `_normalize_windows`, the PowerShell branch, Windows
`_SAFE_KEYS`. Rewrite `tests/test_shell.py`'s list-isolation fixtures to point at
temp YAML.
→ *verify:* full `pytest tests/` green; Phase 1 corpus passes through the real
`shell()` entry point, not just the validator; `grep -rn "blacklist\|whitelist"`
across the repo returns only `modules/ctx_tools`' unrelated token blacklist.

**Phase 3 — fold in the warning list (optional, cuttable).**
Move the 16 `_DESTRUCTIVE` regexes into `deny.yaml` as `action: warn` rules.
They have the same substring-matching defects — `\bgit\s+reset\s+--hard\b` fires
inside a quoted commit message today. Also rewrite `_last_cmd()` (used for
exit-code annotation) to read the last `command` node of the last pipeline
instead of splitting on `|`.
→ *verify:* warning-emission tests for each ported rule + a negative test that a
quoted mention doesn't fire.

**Docs:** `CODEBASE.md`'s `shell` entry (line ~374) and `README.md`'s line 34
both describe the blacklist; update in Phase 2. Bump `EXTENSION_META["version"]`
to `2.0`.

---

## 9. Grammar notes (verified, `tree-sitter-bash` via `tree_sitter` 0.26)

Confirmed parses this plan depends on:

- `echo "i"; echo am; echo harmless` → three sibling `command` nodes under
  `program`, `;` as separate tokens. **No error.**
- `git commit -m "msg with ; and | chars"` → the metacharacters land in
  `string_content`, a leaf under `string`. Data, not structure.
- `echo $(rm -rf /)` → `command_substitution` containing a full `command` node.
  A recursive walk sees `rm` without special casing. Same for `` `id` `` and
  `<(id)`.
- `$CMD arg` and `$(echo rm) -rf /` → `command_name` wrapping a
  `simple_expansion` / `command_substitution` instead of a `word`. Detectable,
  hence §4.1.
- `ls > /etc/passwd` → `redirected_statement` { `command`, `file_redirect` }.
  Redirect targets are reachable for `path_under` rules.
- `ls &` → `&` is a **sibling** of `command` under `program`, not a child.
  The background check must look at siblings.
- `ls -la --color=auto x` → flags arrive as undifferentiated `word` nodes; the
  grammar does **not** split `-la` or `--color=auto`. Flag normalization is our
  job (§5), including POSIX `--` end-of-options (`rm -- -rf` → `-rf` is an
  operand, verified).
- `ls;;;` → `ERROR` node, `has_error == True`. Step 3 catches it.
- Empty / whitespace-only input parses clean to an empty `program` — must be
  rejected explicitly, not by `has_error`.

---

## 10. What this does not do

State plainly, so nobody mistakes the scope:

- **It is not a sandbox.** The container (non-root, read-only rootfs, iptables
  egress rules, internal-only network) is the security boundary. This is defense
  in depth and a UX improvement — a policy layer that stops obvious mistakes and
  stops *lying* about what it blocked.
- **It cannot see runtime values.** `$VAR`, glob expansion, and command
  substitution output are unknowable statically. §4.3's conservative branch is a
  deliberate false-positive trade, not a solution.
- **It does not defeat obfuscation** (§4.2). Base64, encoded payloads,
  interpreter one-liners, and wrapped commands are explicitly out of scope. The
  flat rules ported from the old blocklist catch the unsubtle cases and nothing
  more is claimed.
- **It does not sandbox permitted programs.** If `python foo.py` is allowed, the
  script does whatever it wants. Same for `make`, `npm run`, `git` hooks.
- **It does not track backgrounded processes.** `&` is allowed (§4.5); orphaned
  processes outliving their request remain a known property of the shared
  sandbox container.
- **It does not moderate output.** The existing `whitelist.txt` warning holds:
  allowing `echo` guarantees `echo` can't damage the system, not that a caller
  won't say something objectionable through it.

---

## 11. Resolved decisions

1. **Obfuscation / interpreter escape: out of scope.** No recursive parsing of
   wrapped command strings, no base64 decoding. Flat ported rules only. (§4.2)
2. **Policy load failure blocks everything.** Not configurable. (§7)
3. **Backgrounding stays allowed** at `neutral` — the agent must be able to
   start long-running jobs. (§4.5)
4. **Load once, cache by path.** Policy files are read-only; no hot reload. (§7)
5. **Port the old blocklist and simplify it** — start from `blacklist.txt`'s
   Linux half, collapse the redundant variants (all the `*| bash*` spellings
   become one rule), drop the entries that parsing makes unnecessary, drop the
   PowerShell half entirely.
6. **Tests assert both directions** — every rule needs a must-deny case *and*
   the allow-list needs must-allow cases, so the corpus catches over-blocking as
   well as under-blocking. (§8)

---

## 12. Tiers as data (supersedes §6)

**The flaw §6 shipped with:** rules were data, but the tier *structure* was
code. `_dispatch()` contained

```python
if level < bypass_blacklist:
    if level < neutral:  allow.yaml must permit
    deny.yaml must not object
```

so the system could express any rule and exactly three tiers. Wanting a fourth
("trusted contributor: read-only git, no writes") meant editing Python. The
config names gave it away: `use_whitelist`, `neutral`, `bypass_blacklist` name
the *branches of an if-chain*, not roles that exist in anyone's deployment.
`bypass_blacklist` in particular was a hardcoded escape hatch — special-cased
in code something that should fall out of the data.

**The replacement** is a band table in config:

```yaml
extra:
  shell:
    tiers:
      - {min_level: 30, apply: [builtin:allow, builtin:deny]}
      - {min_level: 45, apply: [builtin:deny]}
      - {min_level: 70, apply: [/instance/trusted-deny.yaml]}
      - {min_level: 90, apply: []}
```

A caller gets the highest band whose `min_level` they meet; every policy in it
must pass. The three constants collapse: `use_whitelist` is the lowest band
(and the tool's `min_permission`), `bypass_blacklist` is `apply: []`, `neutral`
stops existing as a concept.

**What makes it collapse** is that each policy file already declares its own
posture via `default_action`. So `_dispatch()` doesn't need to know which file
is an allow-list and which is a deny-list — it runs a list in order and every
entry must pass. Losing that distinction from the code is the whole trick;
without it, "apply these N policies" wouldn't type-check as a concept.

Details that matter:

- **Removed keys are an error, not ignored.** A config still carrying
  `use_whitelist: 90` blocks the shell with a migration message. Ignoring it
  would silently *loosen* access for exactly the person who had raised it to
  lock things down — a security bug in the worst direction.
- **A broken table blocks every caller, including an `apply: []` band.** If the
  table won't load we don't know which band a caller is in, so we can't know
  they were meant to be unrestricted.
- **Tier `apply` paths must be absolute** (or `builtin:*`). Unlike `extends:`,
  a tier table has no file to resolve a relative path against, and falling back
  to the process CWD would make policy selection depend on how the agent was
  launched.
- `access_backend` stays a scalar. It gates *where* a command runs, not which
  policy applies to it — a genuinely different axis, and folding it into the
  table would have been symmetry for its own sake.

**Known limitation, not addressed.** The axis is still one-dimensional.
`permission_level` answers *who*, but shell risk is *who × where*: the same
user at level 50 gets identical access in a private DM and in a public channel
with 400 people. `modules/memory` hit this and solved it with scopes
(`global` / `guild:x` / `user:bob`) resolved from `SessionEnvironment`, which
this module receives and ignores. Making band selection a function of
`(caller_level, env)` rather than `caller_level` is the next step if that
becomes the pain point; the table is the necessary foundation either way.

---

## 13. Thresholds, not bands (supersedes §12)

§12's band table fixed the right problem the wrong way. Bands made tiers data,
but kept a structure the domain doesn't have: a list of levels, each owning a
nested list of policies. Two things fell out of that.

**It could express nonsense.** Bands `30: [A]`, `45: [A, B]`, `70: [A]` say a
caller at 45 is bound by B and a caller at 70 is not. Policy application should
be monotonic in privilege — anything else is a misconfiguration nobody wants,
and the table happily represented it.

**It buried the actual datum.** What decides whether a policy applies to you is
one number: the level at which you outgrow it. Bands scattered that across the
table (a policy's threshold was implicit in which bands did and didn't list
it), so adding a policy meant editing several entries.

Put the number on the policy instead:

```yaml
min_permission: 30
policies:
  - {policy: builtin:allow, applies_below: 45}
  - {policy: builtin:deny,  applies_below: 90}
```

A caller is subject to every entry whose `applies_below` they are under. Level
20 gets both, 50 gets the deny-list, 95 gets nothing. Adding a policy is one
line. Non-monotonic application is unrepresentable. `min_permission` becomes
its own scalar rather than being inferred from "the lowest band", which it only
ever coincidentally was.

`PolicySet.for_level()` is now a filter over one comparison, and `Band` /
`TierTable` are gone along with the None-vs-empty-tuple distinction they needed
("below every band" versus "unrestricted") — `min_permission` handles the
former in the tool handler, so `()` unambiguously means unrestricted.

### 13.1 The mount bug this exposed

Every instruction written up to §12 — "point it at a file in your instance
directory, mounted read-only" — was **broken under Docker**. compose.yaml
mounted exactly four things (`workspace/`, `data/`, `config.yaml`, `TinyCTX/`),
so an instance-local policy file was not visible inside the container at all.
The feature worked only for bare-metal launches.

Fixed by giving the instance a dedicated extra-config directory:

- `<instance>/config/` on the host, bound **read-only** at `/app/config`.
  Deliberately not the instance directory itself, which holds `.env` and
  `data/` — neither belongs in the container as config.
- `compose_env()` exports the host path as `TINYCTX_CONFIG_DIR` (the bind
  source); compose.yaml sets `TINYCTX_CONFIG_DIR_PATH=/app/config` for the code
  side. Mirrors the existing `TINYCTX_WORKSPACE` / `TINYCTX_WORKSPACE_PATH`
  pair rather than inventing a convention.
- `tinyctx start` and `tinyctx onboard` create it first. Docker auto-creates a
  missing bind source as **root-owned**, which the user then cannot write
  policy files into without sudo.
- `utils/instance.py::runtime_config_dir()` resolves it per environment:
  container env var, else the `TINYCTX_CONFIG_FILE` sibling, else the workspace
  sibling.

That last piece is what makes a **relative** policy name the correct form:

```yaml
policies:
  - {policy: shell-allow.yaml, applies_below: 45}
```

The same `config.yaml` now works on the host and in the container, where that
directory has a different absolute path. §12's "tier paths must be absolute"
rule is reversed — it was right that CWD is not an anchor, but wrong that no
anchor existed. Absolute paths still work and are still on you to keep mounted.


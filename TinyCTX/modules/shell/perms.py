"""
modules/shell/perms.py

Per-command capability tagging for the `shell` tool — docs/PERMISSIONS-PLAN.md
§5. Replaces the old two-tier `applies_below` policy selection
(policy.py/allow.yaml/deny.yaml) as an ACCESS-CONTROL mechanism: capability
decisions ("does this caller have FILE_WRITE / NETWORK_WRITE / ...") now come
from here, checked once by tool_handling.handler.ToolCallHandler via
required_permissions, instead of from a per-level allow/deny rule set.

validate.py's construct/shape checking is UNCHANGED and still runs inside
__main__.py's `_dispatch` — it is what makes injection structurally
impossible (see validate.py's module docstring), which is a different,
complementary concern from "is this capability permitted at all" (§5.2).

Deliberately a MINIMAL table (§5.1). Everything not named below falls
through to UNTRUSTED_EXEC — fail closed, expandable one command at a time as
real usage justifies it (see docs/PERMISSIONS-PLAN.md's "Deliberate
deferrals").
"""
from __future__ import annotations

from typing import Callable

from TinyCTX.permissions import Permission

from .validate import Command, _extract, _get_parser

# ---------------------------------------------------------------------------
# Parsing helper — mirrors validate.check()'s parse step (encode + parse +
# has_error check) but returns the root node (or None) instead of a Verdict,
# since classification doesn't need a rejection message, just "can't tell,
# so worst-case it".
# ---------------------------------------------------------------------------

def _parse(command: str):
    if not command or not command.strip():
        return None
    try:
        source = command.encode("utf-8", errors="surrogateescape")
        root = _get_parser().parse(source).root_node
    except Exception:  # noqa: BLE001 — never let a parser edge case crash the gate
        return None
    if root.has_error:
        return None
    return root


# ---------------------------------------------------------------------------
# Pure computation — no bools at all (redirects still add FILE_WRITE, applied
# generically in classify() below). Exactly allow.yaml's old low tier: with
# no way to name a file, reach the network, or read the environment, a
# pipeline of these can only transform text the caller typed.
# ---------------------------------------------------------------------------

_PURE_COMPUTE = frozenset({
    "echo", "printf", "date", "cal", "expr", "seq", "factor", "basename",
    "dirname", "true", "false", "sleep", "yes",
})

# ---------------------------------------------------------------------------
# Stdin filters — FILE_READ only when they name a file (an operand) rather
# than reading stdin; FILE_WRITE when redirected. A handful have write/read
# flag exceptions the base filter logic can't see (a flag's VALUE isn't
# associated with the flag in validate.Command — see _classify_filter).
# ---------------------------------------------------------------------------

_FILTERS = frozenset({
    "cat", "tr", "rev", "tac", "sort", "uniq", "wc", "head", "tail", "cut",
    "paste", "nl", "grep", "sed", "awk", "cksum", "md5sum", "sha1sum",
    "sha256sum", "shuf",
})

_WRITE_FLAG_EXCEPTIONS: dict[str, frozenset[str]] = {
    "sort": frozenset({"-o"}),
    "shuf": frozenset({"-o"}),
    "sed":  frozenset({"-i"}),
}
_READ_FLAG_EXCEPTIONS: dict[str, frozenset[str]] = {
    "wc": frozenset({"--files0-from"}),
}


def _classify_filter(cmd: Command) -> frozenset[Permission]:
    perms: set[Permission] = set()
    if cmd.operands:                       # named a file rather than reading stdin
        perms.add(Permission.FILE_READ)
    if cmd.redirects:                      # `> file`
        perms.add(Permission.FILE_WRITE)
    if cmd.flags & _WRITE_FLAG_EXCEPTIONS.get(cmd.name or "", frozenset()):
        perms.add(Permission.FILE_WRITE)
    if cmd.flags & _READ_FLAG_EXCEPTIONS.get(cmd.name or "", frozenset()):
        perms.add(Permission.FILE_READ)
    return frozenset(perms)


def _classify_dd(cmd: Command) -> frozenset[Permission]:
    """dd's `if=`/`of=` are plain non-flag tokens (no leading '-'), so
    validate._build() puts them in cmd.operands verbatim — prefix-matchable."""
    perms: set[Permission] = set()
    for op in cmd.operands:
        if op.startswith("of="):
            perms.add(Permission.FILE_WRITE)
        elif op.startswith("if="):
            perms.add(Permission.FILE_READ)
    return frozenset(perms)


# ---------------------------------------------------------------------------
# Always touch the filesystem (§5.1 table).
# ---------------------------------------------------------------------------

_FILE_READ_CMDS  = frozenset({"ls", "find", "stat", "file", "du", "df", "tree", "readlink", "realpath"})
_FILE_WRITE_CMDS = frozenset({"rm", "rmdir", "mkdir", "touch", "truncate", "chmod", "chown", "tee"})
_FILE_RW_CMDS    = frozenset({"cp", "mv", "ln", "install"})

# ---------------------------------------------------------------------------
# Network — classification markers, §6.3.
# ---------------------------------------------------------------------------

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_CURL_DATA_FLAGS = frozenset({
    "-d", "--data", "--data-ascii", "--data-binary", "--data-raw",
    "--data-urlencode", "-F", "--form", "-T", "--upload-file", "--json",
})
_CURL_OUTPUT_FLAGS = frozenset({"-o", "-O", "--output"})
_METHOD_FLAGS = frozenset({"-X", "--request"})


def _classify_curl(cmd: Command) -> frozenset[Permission]:
    perms: set[Permission] = {Permission.NETWORK_READ}
    if cmd.flags & _CURL_DATA_FLAGS:
        perms.add(Permission.NETWORK_WRITE)
    if (cmd.flags & _METHOD_FLAGS) and any(op.upper() in _WRITE_METHODS for op in cmd.operands):
        perms.add(Permission.NETWORK_WRITE)
    if (cmd.flags & _CURL_OUTPUT_FLAGS) or cmd.redirects:
        perms.add(Permission.FILE_WRITE)
    return frozenset(perms)


_WGET_WRITE_FLAGS = frozenset({"--post-data", "--post-file"})
_WGET_OUTPUT_FLAGS = frozenset({"-o", "-O", "--output-document"})


def _classify_wget(cmd: Command) -> frozenset[Permission]:
    perms: set[Permission] = {Permission.NETWORK_READ}
    if cmd.flags & _WGET_WRITE_FLAGS:
        perms.add(Permission.NETWORK_WRITE)
    if "--method" in cmd.flags:
        # The atom strips "=value" (see validate._build), so the actual verb
        # isn't recoverable here — presence of --method at all is treated as
        # a write, conservatively (unknown means blocked, same posture as
        # validate.py's own path checks).
        perms.add(Permission.NETWORK_WRITE)
    if (cmd.flags & _WGET_OUTPUT_FLAGS) or cmd.redirects:
        perms.add(Permission.FILE_WRITE)
    return frozenset(perms)


_GIT_NETWORK_READ_SUBS = frozenset({"clone", "fetch", "pull", "ls-remote"})
_GIT_NETWORK_WRITE_SUBS = frozenset({"push"})


def _classify_git(cmd: Command) -> frozenset[Permission]:
    sub = (cmd.subcommand or "").lower()
    if sub in _GIT_NETWORK_WRITE_SUBS:
        return frozenset({Permission.NETWORK_WRITE})
    if sub in _GIT_NETWORK_READ_SUBS:
        return frozenset({Permission.NETWORK_READ})
    # Every other git subcommand (commit, log, rebase, ...) is local, but git
    # can run hooks and arbitrary aliases — not in the minimal table's
    # network-marker set, so it falls through to the same UNTRUSTED_EXEC
    # every unrecognized command gets.
    return frozenset({Permission.UNTRUSTED_EXEC})


def _is_remote_spec(operand: str) -> bool:
    """Best-effort: does this operand look like a remote scp/rsync/sftp
    target (`user@host:path`, `host:path`, or a URL)? Not a real parser —
    matches the honest-limits posture of docs/PERMISSIONS-PLAN.md §6.4."""
    if "://" in operand:
        return True
    if ":" not in operand:
        return False
    head = operand.split(":", 1)[0]
    if not head or head in (".", ".."):
        return False
    # A bare relative/absolute local path never contains '/' before the ':'
    # in a valid remote spec's host part; a Windows drive letter ("C:\...")
    # is single-char, excluded by len check.
    return "/" not in head and len(head) > 1


def _classify_remote_copy(cmd: Command) -> frozenset[Permission]:
    """scp / rsync / sftp — direction determines FILE_READ+NETWORK_WRITE
    (uploading) vs NETWORK_READ+FILE_WRITE (downloading). Conventionally the
    LAST operand is the destination."""
    operands = cmd.operands
    if not operands:
        return frozenset({Permission.UNTRUSTED_EXEC})
    remote = [_is_remote_spec(o) for o in operands]
    if not any(remote):
        return frozenset({Permission.UNTRUSTED_EXEC})
    if remote[-1]:
        return frozenset({Permission.FILE_READ, Permission.NETWORK_WRITE})
    return frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE})


_PKG_INSTALL_SUBS = frozenset({"install", "add"})


def _classify_pkg_manager(cmd: Command) -> frozenset[Permission]:
    """pip/npm/apt/cargo/gem install is an EXEC, not a fetch — install
    scripts run arbitrary code by design (§6.3). Other subcommands still get
    UNTRUSTED_EXEC, same as any unrecognized command."""
    sub = (cmd.subcommand or "").lower()
    if sub in _PKG_INSTALL_SUBS:
        return frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE, Permission.UNTRUSTED_EXEC})
    return frozenset({Permission.UNTRUSTED_EXEC})


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_STATIC_TAGS: dict[str, frozenset[Permission]] = {
    **{c: frozenset() for c in _PURE_COMPUTE},
    **{c: frozenset({Permission.FILE_READ}) for c in _FILE_READ_CMDS},
    **{c: frozenset({Permission.FILE_WRITE}) for c in _FILE_WRITE_CMDS},
    **{c: frozenset({Permission.FILE_READ, Permission.FILE_WRITE}) for c in _FILE_RW_CMDS},
    "ping":     frozenset({Permission.NETWORK_READ}),
    "dig":      frozenset({Permission.NETWORK_READ}),  # exfil channel — see module docstring / §6.3
    "nslookup": frozenset({Permission.NETWORK_READ}),
    "host":     frozenset({Permission.NETWORK_READ}),
    "http":     frozenset({Permission.NETWORK_READ}),
    "httpie":   frozenset({Permission.NETWORK_READ}),
    "nc":       frozenset({Permission.NETWORK_WRITE}),
    "netcat":   frozenset({Permission.NETWORK_WRITE}),
    "socat":    frozenset({Permission.NETWORK_WRITE}),
    "ssh":      frozenset({Permission.NETWORK_WRITE, Permission.UNTRUSTED_EXEC}),
}

_DYNAMIC_TAGS: dict[str, Callable[[Command], frozenset[Permission]]] = {
    **{c: _classify_filter for c in _FILTERS},
    "dd":     _classify_dd,
    "curl":   _classify_curl,
    "wget":   _classify_wget,
    "git":    _classify_git,
    "scp":    _classify_remote_copy,
    "rsync":  _classify_remote_copy,
    "sftp":   _classify_remote_copy,
    "pip":    _classify_pkg_manager,
    "pip3":   _classify_pkg_manager,
    "npm":    _classify_pkg_manager,
    "apt":    _classify_pkg_manager,
    "apt-get": _classify_pkg_manager,
    "cargo":  _classify_pkg_manager,
    "gem":    _classify_pkg_manager,
}

# Worst case a dynamic invocation of this command could ever need, added
# ADDITIVELY (never replacing the statically-visible tags) when
# Command.dynamic is True — see module docstring's "additive" rule and
# docs/PERMISSIONS-PLAN.md §5's curl example of why replacing would be wrong.
_WORST_CASE: dict[str, frozenset[Permission]] = {
    "curl":    frozenset({Permission.NETWORK_WRITE, Permission.FILE_WRITE}),
    "wget":    frozenset({Permission.NETWORK_WRITE, Permission.FILE_WRITE}),
    "git":     frozenset({Permission.NETWORK_WRITE}),
    "scp":     frozenset({Permission.NETWORK_WRITE, Permission.FILE_READ, Permission.FILE_WRITE}),
    "rsync":   frozenset({Permission.NETWORK_WRITE, Permission.FILE_READ, Permission.FILE_WRITE}),
    "sftp":    frozenset({Permission.NETWORK_WRITE, Permission.FILE_READ, Permission.FILE_WRITE}),
    "pip":     frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE}),
    "pip3":    frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE}),
    "npm":     frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE}),
    "apt":     frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE}),
    "apt-get": frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE}),
    "cargo":   frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE}),
    "gem":     frozenset({Permission.NETWORK_READ, Permission.FILE_WRITE}),
    "sort":    frozenset({Permission.FILE_WRITE}),
    "shuf":    frozenset({Permission.FILE_WRITE}),
    "sed":     frozenset({Permission.FILE_WRITE}),
    "wc":      frozenset({Permission.FILE_READ}),
    "dd":      frozenset({Permission.FILE_READ, Permission.FILE_WRITE}),
}


def classify(cmd: Command) -> frozenset[Permission]:
    """Classify one resolved Command into the permission bools it needs."""
    if cmd.name is None:
        return frozenset({Permission.UNTRUSTED_EXEC})
    base = (
        _DYNAMIC_TAGS[cmd.name](cmd) if cmd.name in _DYNAMIC_TAGS
        else _STATIC_TAGS.get(cmd.name, frozenset({Permission.UNTRUSTED_EXEC}))
    )
    if cmd.redirects:
        # `> file` writes a file regardless of what the command itself is —
        # applies even to the "pure computation" table (§5.1's parenthetical).
        base = base | {Permission.FILE_WRITE}
    if cmd.dynamic:
        # A dynamic argument's real value isn't knowable — worst-case rather
        # than trust whatever flags happen to be statically visible.
        return base | _WORST_CASE.get(cmd.name, frozenset()) | {Permission.UNTRUSTED_EXEC}
    return base


def required_permissions_for_shell(
    command: str, timeout: int | None = None, backend_access: bool = False, **_ignored,
) -> set[Permission]:
    """The `required_permissions` classifier registered for the `shell`
    tool. Consumes the exact Command objects validate._extract already
    produces — no new parsing beyond what shell() itself will reparse via
    check() a moment later for construct/shape validation (§5.2)."""
    root = _parse(command)
    if root is None:
        # Unparseable/empty — can't classify at all, so worst-case it. The
        # friendlier "could not be parsed as bash" message still surfaces
        # from validate.check() inside __main__.py's _dispatch, for callers
        # who DO hold UNTRUSTED_EXEC and reach that far.
        return {Permission.UNTRUSTED_EXEC}
    commands = _extract(root)
    needed: set[Permission] = set()
    for cmd in commands:
        needed |= classify(cmd)
    if backend_access:
        needed.add(Permission.BACKEND_EXEC)
    return needed

"""
perms.py — Per-command capability tagging for the `shell` tool — docs/PERMISSIONS-PLAN.md
§5. Replaces the old two-tier `applies_below` policy selection
(policy.py/allow.yaml/deny.yaml) as an ACCESS-CONTROL mechanism: capability
decisions ("does this caller have FILE_WRITE / NETWORK_WRITE / ...") now come
from perms.yaml (this module compiles and interprets it), checked once by
tool_handling.handler.ToolCallHandler via required_permissions, instead of
from a per-level allow/deny rule set.

validate.py's construct/shape checking is UNCHANGED and still runs inside
__main__.py's `_dispatch` — it is what makes injection structurally
impossible (see validate.py's module docstring), which is a different,
complementary concern from "is this capability permitted at all" (§5.2).

The classification table itself lives in perms.yaml (data), not in this
module (code) — see that file's header for the schema and
docs/PERMISSIONS-PLAN.md §5 for the rationale. This module is the compiler
and interpreter for that data: `_load_table()` parses perms.yaml once (fail
closed — see below), and `classify()` evaluates one resolved Command against
the compiled table. Two direction-dependent tools (scp/rsync/sftp) can't be
expressed by the table's matcher primitives and are deliberately left
unlisted, falling through to the same UNTRUSTED_EXEC every unrecognized
command gets — see perms.yaml's header for why that trade was made instead
of keeping a bespoke Python classifier for just those three.

Fail-closed loading: the table is compiled once, at import time. If
perms.yaml is missing or malformed, that failure is captured (never raised
out of import — a broken data file must not crash the whole shell module)
and `classify()`/`required_permissions_for_shell()` unconditionally return
{Permission.ROOT} for every command until it's fixed — the same "must never
degrade into an unrestricted shell" posture __main__.py already uses for the
shape policy. The error is logged loudly at import time so it's visible
immediately, even though the fail-closed behavior itself is only observable
per-call.

Fail-closed flags, not just fail-closed commands: a flag on a *recognized*
command that isn't declared safe in that entry's `known_flags` (or matched
by one of its `rules:`) adds UNTRUSTED_EXEC too — see classify()'s and
_flag_is_known()'s docstrings. `--help` is the sole universal exemption.
This closes the gap where an entry's base permissions (say, `find`'s bare
FILE_READ) would otherwise silently cover an unvetted flag with a much
bigger effect (`find -delete`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from TinyCTX.permissions import Permission

from .policy import PolicyError
from .validate import Command, _extract, _get_parser

logger = logging.getLogger(__name__)

PERMS_PATH = Path(__file__).parent / "perms.yaml"

_TOP_LEVEL_KEYS = {"version", "extends", "disable", "commands", "worst_case"}
_ENTRY_KEYS = {
    "id", "name", "permissions", "if_operands", "operand_prefix",
    "subcommand", "subcommand_default", "rules", "known_flags",
}
_RULE_KEYS = {"any_flag", "operand_in", "add"}

# Flags treated as harmless on every command, whether or not the entry lists
# them — see classify()'s docstring and _flag_is_known() below. Deliberately
# just --help: -h means "help" on some commands and something else entirely
# on others (ls -h is "human-readable sizes", not help), so it can't be
# assumed safe globally — an entry that wants -h treated as help lists it in
# its own known_flags.
_GLOBALLY_SAFE_FLAGS = frozenset({"--help"})


# ---------------------------------------------------------------------------
# Compiled shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Rule:
    any_flag: frozenset[str] = frozenset()
    operand_in: frozenset[str] = frozenset()
    add: frozenset[Permission] = frozenset()


@dataclass(frozen=True)
class _CommandSpec:
    id: str
    names: frozenset[str]
    permissions: frozenset[Permission] = frozenset()
    if_operands: frozenset[Permission] = frozenset()
    operand_prefix: dict[str, frozenset[Permission]] = field(default_factory=dict)
    subcommand: dict[str, frozenset[Permission]] = field(default_factory=dict)
    subcommand_default: frozenset[Permission] = frozenset()
    rules: tuple[_Rule, ...] = ()
    known_flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Table:
    by_name: dict[str, _CommandSpec]
    worst_case: dict[str, frozenset[Permission]]


# ---------------------------------------------------------------------------
# Compiling — mirrors policy.py's fail-closed posture: any problem with the
# file raises PolicyError, which the caller (module import, below) turns
# into "every command needs ROOT", never into "table is empty, allow
# everything".
# ---------------------------------------------------------------------------

def _as_permission_set(raw, where: str) -> frozenset[Permission]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise PolicyError(f"{where}: expected a list of permission names, got {raw!r}")
    out = set()
    for name in raw:
        try:
            out.add(Permission(str(name)))
        except ValueError:
            valid = ", ".join(sorted(p.value for p in Permission))
            raise PolicyError(
                f"{where}: unknown permission {name!r}. Valid names: {valid}"
            ) from None
    return frozenset(out)


def _as_str_set(raw, where: str) -> frozenset[str]:
    if raw is None:
        return frozenset()
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise PolicyError(f"{where}: expected a string or list of strings, got {raw!r}")
    for v in items:
        if isinstance(v, bool) or not isinstance(v, str):
            # Bare true/false/yes/no/on/off in YAML 1.1 (PyYAML's default
            # resolver) parse as bool, not str — a command literally named
            # "true"/"false"/"yes" (or a flag someone forgot to quote) would
            # otherwise silently vanish from the table instead of loading.
            raise PolicyError(
                f"{where}: {v!r} is not a string — quote it (YAML parses bare "
                "true/false/yes/no/on/off as booleans)"
            )
    return frozenset(items)


def _compile_rule(entry_where: str, raw, index: int) -> _Rule:
    if not isinstance(raw, dict):
        raise PolicyError(f"{entry_where}: rule #{index} must be a mapping")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise PolicyError(f"{entry_where}: rule #{index} has unknown key(s): {sorted(unknown)}")
    any_flag = _as_str_set(raw.get("any_flag"), f"{entry_where}: rule #{index}.any_flag")
    operand_in = frozenset(
        s.upper() for s in _as_str_set(raw.get("operand_in"), f"{entry_where}: rule #{index}.operand_in")
    )
    add = _as_permission_set(raw.get("add"), f"{entry_where}: rule #{index}.add")
    if not any_flag and not operand_in:
        raise PolicyError(
            f"{entry_where}: rule #{index} has no condition (any_flag/operand_in) — "
            "it would always fire; put its permissions in the entry's own 'permissions' instead"
        )
    if not add:
        raise PolicyError(f"{entry_where}: rule #{index} has no 'add' — it would do nothing")
    return _Rule(any_flag=any_flag, operand_in=operand_in, add=add)


def _compile_entry(raw) -> _CommandSpec:
    if not isinstance(raw, dict):
        raise PolicyError(f"perms.yaml: each entry in 'commands' must be a mapping, got {type(raw).__name__}")
    entry_id = raw.get("id")
    if not entry_id or not isinstance(entry_id, str):
        raise PolicyError(f"perms.yaml: every command entry needs a string 'id' (got {raw!r})")
    where = f"perms.yaml: entry {entry_id!r}"

    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise PolicyError(f"{where} has unknown key(s): {sorted(unknown)}")

    names = _as_str_set(raw.get("name"), f"{where}.name")
    if not names:
        raise PolicyError(f"{where} needs a non-empty 'name'")

    operand_prefix_raw = raw.get("operand_prefix") or {}
    if not isinstance(operand_prefix_raw, dict):
        raise PolicyError(f"{where}.operand_prefix must be a mapping")
    operand_prefix = {
        str(prefix): _as_permission_set(perms, f"{where}.operand_prefix[{prefix!r}]")
        for prefix, perms in operand_prefix_raw.items()
    }

    subcommand_raw = raw.get("subcommand") or {}
    if not isinstance(subcommand_raw, dict):
        raise PolicyError(f"{where}.subcommand must be a mapping")
    subcommand = {
        str(sub): _as_permission_set(perms, f"{where}.subcommand[{sub!r}]")
        for sub, perms in subcommand_raw.items()
    }

    subcommand_default = _as_permission_set(raw.get("subcommand_default"), f"{where}.subcommand_default")
    if subcommand_default and not subcommand:
        raise PolicyError(f"{where}.subcommand_default set without 'subcommand'")

    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise PolicyError(f"{where}.rules must be a list")
    rules = tuple(_compile_rule(where, r, i) for i, r in enumerate(rules_raw))

    known_flags = _as_str_set(raw.get("known_flags"), f"{where}.known_flags")

    return _CommandSpec(
        id=entry_id,
        names=names,
        permissions=_as_permission_set(raw.get("permissions"), f"{where}.permissions"),
        if_operands=_as_permission_set(raw.get("if_operands"), f"{where}.if_operands"),
        operand_prefix=operand_prefix,
        subcommand=subcommand,
        subcommand_default=subcommand_default,
        rules=rules,
        known_flags=known_flags,
    )


def _resolve_ref(value, relative_to: Path) -> Path:
    text = str(value)
    if text == "builtin:perms":
        return PERMS_PATH
    target = Path(text).expanduser()
    if target.is_absolute():
        return target
    return relative_to.parent / target


def _compile(path: Path, chain: tuple[str, ...] = ()) -> _Table:
    if not path.exists():
        raise PolicyError(f"perms table not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{path.name} must be a YAML mapping")

    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise PolicyError(f"{path.name}: unknown top-level key(s): {sorted(unknown)}")

    base: _Table | None = None
    if raw.get("extends") is not None:
        base_path = _resolve_ref(raw["extends"], relative_to=path)
        if str(base_path) in chain:
            raise PolicyError(f"{path.name}: circular extends via {base_path}")
        base = _compile(base_path, (*chain, str(path)))

    entries_raw = raw.get("commands") or []
    if not isinstance(entries_raw, list):
        raise PolicyError(f"{path.name}: 'commands' must be a list")
    own_entries = [_compile_entry(e) for e in entries_raw]

    seen_ids: set[str] = set()
    for e in own_entries:
        if e.id in seen_ids:
            raise PolicyError(f"{path.name}: duplicate command entry id {e.id!r}")
        seen_ids.add(e.id)

    disable = raw.get("disable")
    base_entries = list(base.by_name.values()) if base is not None else None
    # by_name.values() can repeat an entry (one per name it covers) — dedupe
    # by id for merge purposes.
    base_by_id: dict[str, _CommandSpec] = {}
    if base is not None:
        for spec in base.by_name.values():
            base_by_id[spec.id] = spec

    if base is None:
        if disable:
            raise PolicyError(f"{path.name}: 'disable' requires 'extends'")
        merged_entries = own_entries
    else:
        own_ids = {e.id for e in own_entries}
        dropped: set[str] = set()
        if disable is not None:
            dropped = {str(d) for d in ([disable] if isinstance(disable, str) else disable)}
            missing = dropped - set(base_by_id)
            if missing:
                raise PolicyError(
                    f"{path.name}: disable lists command entry id(s) not present in the base: "
                    f"{sorted(missing)}"
                )
        kept = [spec for eid, spec in base_by_id.items() if eid not in dropped and eid not in own_ids]
        merged_entries = kept + own_entries

    by_name: dict[str, _CommandSpec] = {}
    for spec in merged_entries:
        for name in spec.names:
            if name in by_name and by_name[name].id != spec.id:
                raise PolicyError(
                    f"{path.name}: command {name!r} is claimed by both "
                    f"{by_name[name].id!r} and {spec.id!r}"
                )
            by_name[name] = spec

    worst_case_raw = raw.get("worst_case") or {}
    if not isinstance(worst_case_raw, dict):
        raise PolicyError(f"{path.name}: 'worst_case' must be a mapping")
    worst_case = dict(base.worst_case) if base is not None else {}
    for name, perms in worst_case_raw.items():
        worst_case[str(name)] = _as_permission_set(perms, f"{path.name}: worst_case[{name!r}]")

    return _Table(by_name=by_name, worst_case=worst_case)


# ---------------------------------------------------------------------------
# Load once at import time. Fail closed: a bad file never raises out of
# import (that would crash the whole shell module for an unrelated typo in
# data) — it's captured here and every classification returns {ROOT} until
# fixed. See module docstring.
# ---------------------------------------------------------------------------

_table: _Table | None = None
_load_error: str | None = None

try:
    _table = _compile(PERMS_PATH)
except PolicyError as exc:
    _load_error = str(exc)
    logger.error("shell: perms.yaml failed to load — every command will require ROOT: %s", exc)


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
# Evaluation
# ---------------------------------------------------------------------------

def _flag_is_known(flag: str, known: frozenset[str]) -> bool:
    """Is `flag` (one member of Command.flags — see validate.py's _build)
    accounted for by `known`? Direct membership only — deliberately no
    cluster decomposition.

    A combined short-flag cluster like `-la` is its own distinct member of
    `.flags` (alongside its atoms `-l`/`-a`, per validate.py's _build), so a
    command entry that wants `-la` recognized lists `-la` itself, not just
    `-l` and `-a` separately.

    This was tried the other way first — treat a flag as known if every
    character it decomposes into is individually known — and it's a real
    trap: validate.py decomposes ANY single-dash multi-char token the same
    way, whether it's a genuine POSIX cluster (`-la` = `-l` + `-a`) or a
    single-dash GNU "spelled out" long option (find's `-delete`, `-exec`,
    `-ok`). `-delete` decomposes into {-d,-e,-l,-t}; on a command whose
    known_flags already legitimately contains `-d`/`-e`/`-l`/`-t` for
    unrelated reasons (they're common short flags elsewhere in the same
    entry), decomposition silently waved `-delete` through as "known" —
    exactly the flag this mechanism most needs to catch. There is no
    reliable way to tell a cluster from a spelled-out word using only
    validate.py's Command fields, so: no decomposition, register the
    combined spellings you actually want to allow.
    """
    return flag in known


def _unaccounted_flags(spec: _CommandSpec, cmd: Command) -> frozenset[str]:
    """Flags on `cmd` that aren't declared safe (known_flags), aren't
    referenced by any of this entry's `rules:` conditions (their effect is
    already accounted for in what those rules add), and aren't globally
    exempt. See classify()'s docstring."""
    accounted = spec.known_flags | _GLOBALLY_SAFE_FLAGS
    for rule in spec.rules:
        accounted |= rule.any_flag
    return frozenset(f for f in cmd.flags if not _flag_is_known(f, accounted))


def _eval_spec(spec: _CommandSpec, cmd: Command) -> frozenset[Permission]:
    perms: set[Permission] = set(spec.permissions)

    if spec.subcommand:
        sub = (cmd.subcommand or "").lower()
        perms |= spec.subcommand.get(sub, spec.subcommand_default)

    if cmd.operands and spec.if_operands:
        perms |= spec.if_operands

    for prefix, add in spec.operand_prefix.items():
        if any(op.startswith(prefix) for op in cmd.operands):
            perms |= add

    for rule in spec.rules:
        flag_ok = not rule.any_flag or bool(cmd.flags & rule.any_flag)
        operand_ok = not rule.operand_in or any(op.upper() in rule.operand_in for op in cmd.operands)
        if flag_ok and operand_ok:
            perms |= rule.add

    if _unaccounted_flags(spec, cmd):
        # A flag this table doesn't recognize for this command — its effect
        # is unknown, so worst-case it rather than silently letting it ride
        # along on whatever base permissions the command already has.
        perms.add(Permission.UNTRUSTED_EXEC)

    return frozenset(perms)


def classify(cmd: Command) -> frozenset[Permission]:
    """Classify one resolved Command into the permission bools it needs.

    Every flag on a *recognized* command is itself checked: one that isn't
    declared safe via that entry's `known_flags`, and isn't the flag a
    `rules:` condition already accounts for, adds UNTRUSTED_EXEC — an
    unregistered flag's effect is unknown, so it's worst-cased rather than
    silently riding along on the command's base permissions. `--help` is
    the one flag exempt on every command (see _GLOBALLY_SAFE_FLAGS); nothing
    else is assumed safe without being named. An *unrecognized command*
    already gets UNTRUSTED_EXEC unconditionally, so this check only matters
    for commands that ARE in the table.
    """
    if _load_error is not None:
        return frozenset({Permission.ROOT})
    if cmd.name is None:
        return frozenset({Permission.UNTRUSTED_EXEC})

    table = _table
    spec = table.by_name.get(cmd.name)
    base = _eval_spec(spec, cmd) if spec is not None else frozenset({Permission.UNTRUSTED_EXEC})

    if cmd.redirects:
        # `> file` writes a file regardless of what the command itself is —
        # applies even to commands with no other permissions at all.
        base = base | {Permission.FILE_WRITE}

    if cmd.dynamic:
        # A dynamic argument's real value isn't knowable — worst-case rather
        # than trust whatever flags happen to be statically visible.
        return base | table.worst_case.get(cmd.name, frozenset()) | {Permission.UNTRUSTED_EXEC}

    return base


def required_permissions_for_shell(
    command: str, timeout: int | None = None, backend_access: bool = False, **_ignored,
) -> set[Permission]:
    """The `required_permissions` classifier registered for the `shell`
    tool. Consumes the exact Command objects validate._extract already
    produces — no new parsing beyond what shell() itself will reparse via
    check() a moment later for construct/shape validation (§5.2)."""
    if _load_error is not None:
        return {Permission.ROOT}

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

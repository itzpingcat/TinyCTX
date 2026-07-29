"""
modules/shell/policy.py

Loads and compiles the shell command policy from YAML. Knows nothing about
tree-sitter — it turns a file into `Policy`/`Rule` dataclasses and validates
that the file is well-formed. `validate.py` consumes the result.

Two postures, declared by the file's own `default_action`:

  default_action: allow   — deny-list ("run anything except these"). Every rule
                            must be action deny or warn. Used by deny.yaml for
                            callers at/above permissions.neutral.

  default_action: deny    — allow-list ("run nothing except these"). Every rule
                            must be action allow. Used by allow.yaml for callers
                            below permissions.neutral.

Fail-closed by design:
  - A missing or malformed file raises PolicyError. The caller turns that into
    a blocked command, NOT an unrestricted shell. (The old blacklist.txt did the
    opposite: a missing file logged a warning and let everything through.)
  - Unknown keys in a rule are an error, not ignored. A typo'd `commnad:` would
    otherwise silently drop the constraint and leave a rule that matches every
    command.
  - `constructs` is an explicit allow-map. validate.py denies any bash syntax
    node not listed there, so a tree-sitter-bash grammar upgrade that adds node
    types fails closed.

Inheritance:
  A policy may `extends:` another instead of restating it, so adding one rule
  to the shipped defaults doesn't mean vendoring the whole file:

      extends: builtin:allow      # or builtin:deny, or a path
      rules:
        - {id: ps, action: allow, command: ps, ...}
      disable: [some-base-rule-id]

  Base rules come first, then the extending file's. A rule with the same `id`
  as a base rule replaces it; `disable:` drops base rules by id. Both
  `default_action` and `constructs` are inherited, and declared constructs
  merge over the base so a single node type can be flipped on its own. A file
  cannot change the inherited `default_action` — allow-posture and
  deny-posture rules use different schemas.

Policies are loaded once and cached by (path, workspace). The files are
read-only by design (mounted read-only into the container) and are not meant to
be edited hot — changing one requires a restart.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DENY_PATH = Path(__file__).parent / "deny.yaml"
ALLOW_PATH = Path(__file__).parent / "allow.yaml"

# Interpolated into path_under / path_outside values at load time.
_WORKSPACE_TOKEN = "${workspace}"

_ACTIONS = {"allow", "deny", "warn"}
_CONSTRUCT_VALUES = {"allow", "deny"}

# Matcher keys legal on a rule, by the posture of the file it lives in.
_DENY_RULE_KEYS = {
    "id", "action", "message",
    "command", "subcommand", "any_flag", "all_flags", "no_operands",
    "path_under", "path_outside", "redirect_under",
}
_ALLOW_RULE_KEYS = {
    "id", "action", "message",
    "command", "subcommand", "allowed_flags", "arg_matches", "max_args",
}
_TOP_LEVEL_KEYS = {
    "version", "default_action", "constructs", "defaults", "rules",
    "extends", "disable",
}

DEFAULT_MAX_COMMAND_BYTES = 8192


class PolicyError(Exception):
    """Policy file missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class Rule:
    """One rule. Matches a single resolved command (see validate.Command).

    Deny-posture rules use command/subcommand/any_flag/all_flags/no_operands/
    path_under/path_outside/redirect_under.
    Allow-posture rules use command/subcommand/allowed_flags/arg_matches/max_args.
    A field left None is simply not part of the match.

    Flag matching is asymmetric on purpose. Deny rules match Command.flags,
    which holds both the canonical atoms and the tokens as written, so a rule
    can name either `-rf` or `-r`, and single-dash long options (`-delete`)
    work. Allow rules match Command.atoms, the canonical POSIX reading, so an
    allow-list author writes `[-l, -a]` once and it covers `-l -a`, `-la`, and
    `-al` without listing every cluster spelling.
    """

    id: str
    action: str
    message: str
    command: frozenset[str] | None = None
    subcommand: frozenset[str] | None = None
    any_flag: frozenset[str] | None = None
    all_flags: frozenset[str] | None = None
    no_operands: bool = False
    allowed_flags: frozenset[str] | None = None
    arg_matches: re.Pattern | None = None
    max_args: int | None = None
    path_under: tuple[str, ...] | None = None
    path_outside: tuple[str, ...] | None = None
    redirect_under: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Policy:
    name: str
    default_action: str
    constructs: dict[str, str]
    rules: tuple[Rule, ...]
    max_command_bytes: int


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_cache: dict[tuple[str, str], Policy] = {}


def load_policy(path: Path | str, workspace: Path | str) -> Policy:
    """Load and compile a policy file. Cached by (path, workspace).

    Raises PolicyError on anything wrong with the file. Callers must treat that
    as "block everything" — never as "no policy, allow everything".
    """
    path = Path(path)
    key = (str(path), str(workspace))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    policy = _compile(path, str(workspace))
    _cache[key] = policy
    logger.debug(
        "shell: loaded policy %s (%s, %d rules)",
        path.name, policy.default_action, len(policy.rules),
    )
    return policy


def clear_cache() -> None:
    """Drop the policy cache. Used by tests."""
    _cache.clear()


def _resolve_extends(value, child: Path) -> Path:
    """Resolve an `extends:` target.

    "builtin:allow" / "builtin:deny" name the policies shipped in this module —
    the common case, so an operator can add one rule without vendoring the
    whole file. Anything else is a path, resolved relative to the file doing
    the extending.
    """
    text = str(value)
    if text == "builtin:allow":
        return ALLOW_PATH
    if text == "builtin:deny":
        return DENY_PATH
    target = Path(text).expanduser()
    return target if target.is_absolute() else (child.parent / target)


def _compile(path: Path, workspace: str, chain: tuple[str, ...] = ()) -> Policy:
    if not path.exists():
        raise PolicyError(f"policy file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{path.name} must be a YAML mapping")

    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise PolicyError(f"{path.name}: unknown top-level key(s): {sorted(unknown)}")

    base: Policy | None = None
    if raw.get("extends") is not None:
        base_path = _resolve_extends(raw["extends"], path)
        if str(base_path) in chain:
            raise PolicyError(f"{path.name}: circular extends via {base_path}")
        base = _compile(base_path, workspace, (*chain, str(path)))

    default_action = raw.get("default_action") or (base.default_action if base else None)
    if default_action not in {"allow", "deny"}:
        raise PolicyError(
            f"{path.name}: default_action must be 'allow' or 'deny', got {default_action!r}"
        )
    if base is not None and default_action != base.default_action:
        raise PolicyError(
            f"{path.name}: cannot extend a default_action={base.default_action} policy "
            f"with default_action={default_action} — the rule schemas are different"
        )

    # An extending file may omit constructs entirely; what it does declare is
    # merged over the base, so a single key can be flipped without restating
    # the whole map.
    constructs = _compile_constructs(path, raw.get("constructs"), base)
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise PolicyError(f"{path.name}: defaults must be a mapping")
    inherited_bytes = base.max_command_bytes if base else DEFAULT_MAX_COMMAND_BYTES
    max_bytes = int(defaults.get("max_command_bytes", inherited_bytes))
    if max_bytes < 1:
        raise PolicyError(f"{path.name}: max_command_bytes must be positive")

    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise PolicyError(f"{path.name}: rules must be a list")

    legal_keys = _ALLOW_RULE_KEYS if default_action == "deny" else _DENY_RULE_KEYS
    seen_ids: set[str] = set()
    own_rules = []
    for entry in rules_raw:
        rule = _compile_rule(path, entry, default_action, legal_keys, workspace)
        if rule.id in seen_ids:
            raise PolicyError(f"{path.name}: duplicate rule id {rule.id!r}")
        seen_ids.add(rule.id)
        own_rules.append(rule)

    rules = _merge_rules(path, base, own_rules, seen_ids, raw.get("disable"))

    return Policy(
        name=path.name,
        default_action=default_action,
        constructs=constructs,
        rules=tuple(rules),
        max_command_bytes=max_bytes,
    )


def _merge_rules(path: Path, base: Policy | None, own: list[Rule],
                 own_ids: set[str], disable) -> list[Rule]:
    """Base rules first, then this file's. Same id replaces; `disable:` drops.

    Order matters for deny policies: `action: warn` rules must still be
    reachable after any `action: deny` rule that would short-circuit, and base
    rules keeping their original relative order preserves that.
    """
    if base is None:
        if disable:
            raise PolicyError(f"{path.name}: 'disable' requires 'extends'")
        return own

    dropped = set()
    if disable is not None:
        dropped = {str(d) for d in ([disable] if isinstance(disable, str) else disable)}
        base_ids = {r.id for r in base.rules}
        missing = dropped - base_ids
        if missing:
            # A typo here would silently leave the rule in force, which is the
            # opposite of what the operator asked for.
            raise PolicyError(
                f"{path.name}: disable lists rule id(s) not in {base.name}: {sorted(missing)}"
            )

    kept = [r for r in base.rules if r.id not in dropped and r.id not in own_ids]
    return kept + own


def _compile_constructs(path: Path, raw, base: Policy | None = None) -> dict[str, str]:
    if raw is None and base is not None:
        return dict(base.constructs)
    if not isinstance(raw, dict) or not raw:
        raise PolicyError(
            f"{path.name}: a non-empty 'constructs' mapping is required — "
            "syntax not listed there is denied"
        )
    out = dict(base.constructs) if base is not None else {}
    for node_type, value in raw.items():
        if value not in _CONSTRUCT_VALUES:
            raise PolicyError(
                f"{path.name}: constructs.{node_type} must be 'allow' or 'deny', got {value!r}"
            )
        out[str(node_type)] = value
    return out


def _compile_rule(path: Path, entry, default_action: str, legal_keys: set[str], workspace: str) -> Rule:
    if not isinstance(entry, dict):
        raise PolicyError(f"{path.name}: each rule must be a mapping, got {type(entry).__name__}")

    rule_id = entry.get("id")
    if not rule_id or not isinstance(rule_id, str):
        raise PolicyError(f"{path.name}: every rule needs a string 'id' (got {entry!r})")

    unknown = set(entry) - legal_keys
    if unknown:
        raise PolicyError(
            f"{path.name}: rule {rule_id!r} has key(s) not valid in a "
            f"default_action={default_action} file: {sorted(unknown)}"
        )

    action = entry.get("action")
    if action not in _ACTIONS:
        raise PolicyError(f"{path.name}: rule {rule_id!r} action must be one of {sorted(_ACTIONS)}")
    if default_action == "deny" and action != "allow":
        raise PolicyError(
            f"{path.name}: rule {rule_id!r} — an allow-list file may only contain action: allow"
        )
    if default_action == "allow" and action == "allow":
        raise PolicyError(
            f"{path.name}: rule {rule_id!r} — a deny-list file may only contain "
            "action: deny or action: warn"
        )

    message = entry.get("message") or rule_id

    arg_matches = None
    if "arg_matches" in entry:
        try:
            arg_matches = re.compile(entry["arg_matches"])
        except re.error as exc:
            raise PolicyError(
                f"{path.name}: rule {rule_id!r} arg_matches is not a valid regex: {exc}"
            ) from exc

    max_args = entry.get("max_args")
    if max_args is not None:
        max_args = int(max_args)
        if max_args < 0:
            raise PolicyError(f"{path.name}: rule {rule_id!r} max_args must be >= 0")

    rule = Rule(
        id=rule_id,
        action=action,
        message=message,
        command=_as_set(entry.get("command"), lower=True),
        subcommand=_as_set(entry.get("subcommand")),
        any_flag=_as_set(entry.get("any_flag")),
        all_flags=_as_set(entry.get("all_flags")),
        no_operands=bool(entry.get("no_operands", False)),
        allowed_flags=_as_set(entry.get("allowed_flags")),
        arg_matches=arg_matches,
        max_args=max_args,
        path_under=_as_paths(entry.get("path_under"), workspace),
        path_outside=_as_paths(entry.get("path_outside"), workspace),
        redirect_under=_as_paths(entry.get("redirect_under"), workspace),
    )

    if _is_unconstrained(rule):
        raise PolicyError(
            f"{path.name}: rule {rule_id!r} has no matcher fields — it would "
            "match every command"
        )
    return rule


def _is_unconstrained(rule: Rule) -> bool:
    return not any((
        rule.command, rule.subcommand, rule.any_flag, rule.all_flags,
        rule.no_operands, rule.allowed_flags is not None, rule.arg_matches,
        rule.max_args is not None, rule.path_under, rule.path_outside,
        rule.redirect_under,
    ))


def _as_set(value, lower: bool = False) -> frozenset[str] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value)
    return frozenset(str(v).lower() if lower else str(v) for v in items)


def _as_paths(value, workspace: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value)
    return tuple(str(v).replace(_WORKSPACE_TOKEN, workspace) for v in items)

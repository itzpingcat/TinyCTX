"""
modules/shell/validate.py

Parses a bash command with tree-sitter-bash and checks the resulting AST against
a compiled Policy. Pure functions over data — no file I/O, no config, no logging
of command text.

Why an AST instead of matching the command string:

    echo "i"; echo "am"; echo "harmless"

is three independent `command` nodes, each checked on its own — so a rule about
`rm` cannot be tripped by the word "rm" appearing in someone else's argument.
And in

    git commit -m "msg with ; and | chars"

the metacharacters arrive as `string_content`, a parser leaf. Data, not
structure. That is what retires the old `{arg}` character-class hack: at the
allow-list tier every argument is a leaf, so injection is not filtered, it is
structurally unrepresentable.

Three checks, all fail-closed, in order:

  1. Parse. Empty input, oversized input, or any ERROR/MISSING node -> denied.
  2. Constructs. Every *named* node type must be mapped to "allow" in the
     policy's constructs table. Unmapped node types are denied, so a
     tree-sitter-bash grammar upgrade that introduces new syntax fails closed.
     Anonymous tokens (`;`, `|`, `then`, ...) are governed by their named
     parent; the bare `&` token is the one exception and is checked under the
     synthetic key "background".
  3. Rules. Every `command` node in the tree — including those nested inside
     `$(...)`, `<(...)`, subshells, and loop bodies, which a single recursive
     walk reaches for free — is resolved to a Command and matched.

Explicitly NOT attempted (see PLAN.md 4.2): defeating obfuscation. No recursive
re-parsing of `bash -c "..."` payloads, no base64 decoding, no interpreter
source analysis. The container is the security boundary; this is defense in
depth. The flat rules ported from the old blocklist catch the unsubtle cases and
nothing more is claimed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .policy import Policy, Rule

# Node types whose presence anywhere under an argument means its runtime value
# is not knowable statically. See PLAN.md 4.3.
_EXPANSION_TYPES = frozenset({
    "simple_expansion",
    "expansion",
    "command_substitution",
    "process_substitution",
    "arithmetic_expansion",
})

# Quoted-literal node types — their text includes the quote characters.
_QUOTED_TYPES = frozenset({"string", "raw_string", "ansi_c_string", "translated_string"})

# Unquoted glob characters make a word's expansion unknowable, same as $VAR.
_GLOB_CHARS = "*?["

# Synthetic construct key for the bare `&` token, which has no named wrapper.
_BACKGROUND = "background"

_parser = None


def _get_parser():
    """Build the tree-sitter parser once, lazily.

    Import is deferred so that merely importing this module (e.g. during test
    collection) doesn't hard-require the grammar package.
    """
    global _parser
    if _parser is None:
        import tree_sitter_bash
        from tree_sitter import Language, Parser

        _parser = Parser(Language(tree_sitter_bash.language()))
    return _parser


@dataclass(frozen=True)
class Command:
    """One resolved command node.

    name        executable basename, or None when it isn't a literal word
                (`$CMD arg`, `$(echo rm) -rf /`) — see PLAN.md 4.1.
    atoms       canonical POSIX reading of the flags: `-la` -> {-l, -a},
                `--x=y` -> {--x}. What allow rules match against.
    flags       atoms PLUS the tokens as written, so `-la` also yields {-la} and
                `find -delete` yields {-delete} alongside its (meaningless)
                character split. Deny rules match against this wider set: a deny
                rule should fire on either spelling, and single-dash long
                options like `-delete` only exist here.
    operands    non-flag arguments, quotes stripped. Everything after a bare
                `--` is an operand, never a flag.
    redirects   redirect targets attached to this command (`> /etc/passwd`).
    dynamic     True if any operand or redirect target contains an expansion or
                an unquoted glob, i.e. its value isn't knowable statically.
    """

    name: str | None
    atoms: frozenset[str]
    flags: frozenset[str]
    operands: tuple[str, ...]
    redirects: tuple[str, ...]
    dynamic: bool

    @property
    def subcommand(self) -> str | None:
        return self.operands[0] if self.operands else None


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def check(command: str, policy: Policy, workspace: Path | str) -> Verdict:
    """Check `command` against `policy`. Never raises on malformed input."""
    if not command.strip():
        return Verdict(False, "empty command")

    source = command.encode("utf-8", errors="surrogateescape")
    if len(source) > policy.max_command_bytes:
        return Verdict(
            False,
            f"command is {len(source)} bytes, over the "
            f"{policy.max_command_bytes}-byte limit",
        )

    root = _get_parser().parse(source).root_node
    if root.has_error:
        return Verdict(False, "could not be parsed as bash (syntax error)")

    bad = _first_denied_construct(root, policy)
    if bad is not None:
        return Verdict(False, f"bash construct '{bad}' is not permitted at this permission level")

    workspace = Path(workspace)
    commands = _extract(root)

    if not commands and policy.default_action == "deny":
        return Verdict(False, "no runnable command found")

    warnings: list[str] = []
    for cmd in commands:
        if cmd.name is None:
            return Verdict(
                False,
                "command name is not a literal word — a command built from a "
                "variable or substitution cannot be checked",
            )
        if policy.default_action == "deny":
            if not any(_allow_permits(r, cmd) for r in policy.rules):
                return Verdict(False, f"'{cmd.name}' is not in the allow-list")
            continue
        for rule in policy.rules:
            if not _deny_fires(rule, cmd, workspace):
                continue
            if rule.action == "deny":
                return Verdict(False, f"[{rule.id}] {rule.message}")
            warnings.append(f"warning: {rule.message}")

    return Verdict(True, warnings=tuple(dict.fromkeys(warnings)))


# ---------------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------------

def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _first_denied_construct(root, policy: Policy) -> str | None:
    """Return the first node type the policy doesn't map to "allow", else None."""
    for node in _walk(root):
        if not node.is_named:
            # `&&` and `||` are single tokens, so a bare `&` is unambiguously
            # backgrounding.
            if node.type == "&" and policy.constructs.get(_BACKGROUND) != "allow":
                return _BACKGROUND
            continue
        if policy.constructs.get(node.type) != "allow":
            return node.type
    return None


def _extract(root) -> list[Command]:
    """Collect every `command` node in the tree, with redirects attached."""
    redirects: dict[int, list[str]] = {}
    for node in _walk(root):
        if node.type != "redirected_statement":
            continue
        body = next((c for c in node.children if c.type == "command"), None)
        if body is None:
            continue
        targets = redirects.setdefault(body.id, [])
        for child in node.children:
            if child.type in ("file_redirect", "heredoc_redirect"):
                target = _redirect_target(child)
                if target is not None:
                    targets.append(target)

    return [
        _build(node, redirects.get(node.id, ()))
        for node in _walk(root)
        if node.type == "command"
    ]


def _redirect_target(redirect) -> str | None:
    named = [c for c in redirect.children if c.is_named and c.type != "file_descriptor"]
    return _literal(named[-1]) if named else None


def last_command_name(command: str) -> str:
    """Basename of the last command in the input, or "" if not resolvable.

    Used for exit-code interpretation: a pipeline exits with its last command's
    status, so `find . | head` should be read as head's exit code, not find's.
    Replaces a hand-rolled split on `|`, which mistook a `|` inside a quoted
    argument for a pipe.
    """
    if not command.strip():
        return ""
    try:
        root = _get_parser().parse(command.encode("utf-8", errors="surrogateescape")).root_node
    except Exception:  # noqa: BLE001 — annotation is cosmetic, never fail a result on it
        return ""
    last = None
    for node in _walk(root):
        if node.type == "command":
            last = node
    return (_command_name(last) or "") if last is not None else ""


def _command_name(node) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    inner = name_node.children[0] if name_node.children else name_node
    if inner.type != "word":
        return None
    return inner.text.decode("utf-8", "replace").split("/")[-1].lower()


def _build(node, redirect_targets) -> Command:
    name = _command_name(node)
    atoms: set[str] = set()
    raw_flags: set[str] = set()
    operands: list[str] = []
    dynamic = False
    end_of_flags = False

    for child in node.children:
        # Compare by type, not identity: the bindings hand out a fresh Node
        # wrapper per call, so `child is name_node` is never true and the
        # command name would be counted as its own first operand.
        if child.type in ("command_name", "variable_assignment") or not child.is_named:
            continue
        text = _literal(child)
        if _is_dynamic(child):
            dynamic = True
            operands.append(text)
            continue
        if end_of_flags:
            operands.append(text)
            continue
        if text == "--":
            end_of_flags = True
            continue
        # `-3` in `head -3` parses as a number, not a flag.
        if child.type == "number" or not text.startswith("-") or text == "-":
            operands.append(text)
            continue
        raw_flags.add(text)
        if text.startswith("--"):
            atoms.add(text.split("=", 1)[0])
        else:
            atoms.update("-" + ch for ch in text[1:])

    for target in redirect_targets:
        if any(ch in target for ch in _GLOB_CHARS):
            dynamic = True

    return Command(
        name=name,
        atoms=frozenset(atoms),
        flags=frozenset(atoms | raw_flags),
        operands=tuple(operands),
        redirects=tuple(redirect_targets),
        dynamic=dynamic,
    )


def _literal(node) -> str:
    """Node text with surrounding quotes removed where they're syntax."""
    if node.type in _QUOTED_TYPES:
        parts = [c.text.decode("utf-8", "replace") for c in node.children if c.type == "string_content"]
        if parts:
            return "".join(parts)
        text = node.text.decode("utf-8", "replace")
        if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
            return text[1:-1]
        return text
    return node.text.decode("utf-8", "replace")


def _is_dynamic(node) -> bool:
    if any(n.type in _EXPANSION_TYPES for n in _walk(node)):
        return True
    # Globs only expand outside quotes.
    if node.type in _QUOTED_TYPES:
        return False
    return any(ch in node.text.decode("utf-8", "replace") for ch in _GLOB_CHARS)


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

def _deny_fires(rule: Rule, cmd: Command, workspace: Path) -> bool:
    """True if a deny/warn rule matches. Every present field must match (AND).

    A value-dependent field (path_under / path_outside) fires when the command
    has a dynamic argument, because the value can't be checked. Conservative by
    construction: unknown means blocked, not allowed.
    """
    if rule.command is not None and (cmd.name is None or cmd.name not in rule.command):
        return False
    if rule.subcommand is not None and (cmd.subcommand or "") not in rule.subcommand:
        return False
    if rule.any_flag is not None and not (cmd.flags & rule.any_flag):
        return False
    if rule.all_flags is not None and not rule.all_flags <= cmd.flags:
        return False
    if rule.no_operands and cmd.operands:
        return False

    if rule.redirect_under is not None:
        targets = [_normalize(t, workspace) for t in cmd.redirects]
        if not any(_under(t, rule.redirect_under) for t in targets):
            return False

    if rule.path_under is not None and not _path_hit(cmd, workspace, rule.path_under, inside=True):
        return False
    if rule.path_outside is not None and not _path_hit(cmd, workspace, rule.path_outside, inside=False):
        return False
    return True


def _path_hit(cmd: Command, workspace: Path, prefixes: tuple[str, ...], inside: bool) -> bool:
    """Whether any operand or redirect target satisfies the path test.

    A dynamic argument always counts as a hit. Its value isn't knowable, and
    unknown has to mean blocked — a `$VAR` that might be `/etc` must not slip
    past a rule that would have caught the literal.
    """
    if cmd.dynamic:
        return True
    paths = _paths(cmd, workspace)
    if inside:
        return any(_under(p, prefixes) for p in paths)
    return any(not _under(p, prefixes) for p in paths)


def _allow_permits(rule: Rule, cmd: Command) -> bool:
    """True if an allow rule fully covers `cmd`.

    Unlike a deny rule, this is a complete-coverage test: the rule must account
    for the command's subcommand, every flag, and every operand.
    """
    if cmd.dynamic:
        return False
    if rule.command is not None and (cmd.name is None or cmd.name not in rule.command):
        return False
    if rule.subcommand is not None and (cmd.subcommand or "") not in rule.subcommand:
        return False
    if rule.allowed_flags is not None and not cmd.atoms <= rule.allowed_flags:
        return False
    if rule.allowed_flags is None and cmd.atoms:
        return False
    if cmd.redirects:
        return False

    operands = cmd.operands
    if rule.subcommand is not None:
        operands = operands[1:]
    if rule.max_args is not None and len(operands) > rule.max_args:
        return False
    if rule.max_args is None and operands:
        return False
    if rule.arg_matches is not None:
        return all(rule.arg_matches.search(a) for a in operands)
    return True


def _paths(cmd: Command, workspace: Path) -> list[str]:
    return [_normalize(a, workspace) for a in (*cmd.operands, *cmd.redirects)]


def _normalize(text: str, workspace: Path) -> str:
    """Lexical path normalization — no filesystem access, no symlink resolution.

    The validator runs in the agent container; the command runs somewhere else
    (sandbox container). Resolving against *this* filesystem would be wrong. See
    PLAN.md 4.6 for what this consequently does not catch.
    """
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        expanded = os.path.join(str(workspace), expanded)
    return os.path.normpath(expanded)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        prefix = os.path.normpath(prefix)
        if path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep):
            return True
    return False

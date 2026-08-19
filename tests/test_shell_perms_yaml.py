"""
tests/test_shell_perms_yaml.py

Tests for modules/shell/perms.py's YAML compiler — the loader that turns
perms.yaml (or an extending instance file) into the table classify() uses.
Complements test_shell_perms.py, which exercises classify()'s *behavior*
against the real shipped perms.yaml; this file exercises the *loader*:
schema validation, extends:/disable: layering, and the fail-closed posture
on a broken file.

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.modules.shell import perms as perms_mod
from TinyCTX.modules.shell.policy import PolicyError
from TinyCTX.permissions import Permission


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The shipped file loads clean, at import time, with no error captured.
# ---------------------------------------------------------------------------

def test_shipped_perms_yaml_loads_without_error():
    assert perms_mod._load_error is None
    assert perms_mod._table is not None


def test_shipped_perms_yaml_covers_expected_commands():
    for name in ("ls", "rm", "cp", "git", "curl", "wget", "dd", "sort", "sed", "wc", "pip", "npm"):
        assert name in perms_mod._table.by_name, f"{name!r} missing from compiled table"


def test_scp_family_deliberately_unlisted():
    for name in ("scp", "rsync", "sftp"):
        assert name not in perms_mod._table.by_name


# ---------------------------------------------------------------------------
# Schema validation — fail closed on anything malformed.
# ---------------------------------------------------------------------------

def test_missing_file_raises():
    with pytest.raises(PolicyError, match="not found"):
        perms_mod._compile(__import__("pathlib").Path("/nonexistent/perms.yaml"))


def test_not_a_mapping_raises(tmp_path):
    p = _write(tmp_path, "perms.yaml", "- just\n- a\n- list\n")
    with pytest.raises(PolicyError, match="must be a YAML mapping"):
        perms_mod._compile(p)


def test_unknown_top_level_key_raises(tmp_path):
    p = _write(tmp_path, "perms.yaml", "version: 1\nbogus: true\ncommands: []\n")
    with pytest.raises(PolicyError, match="unknown top-level key"):
        perms_mod._compile(p)


def test_entry_without_id_raises(tmp_path):
    p = _write(tmp_path, "perms.yaml", "version: 1\ncommands:\n  - name: ls\n    permissions: [file_read]\n")
    with pytest.raises(PolicyError, match="needs a string 'id'"):
        perms_mod._compile(p)


def test_entry_without_name_raises(tmp_path):
    p = _write(tmp_path, "perms.yaml", "version: 1\ncommands:\n  - id: x\n    permissions: [file_read]\n")
    with pytest.raises(PolicyError, match="needs a non-empty 'name'"):
        perms_mod._compile(p)


def test_unknown_permission_name_raises(tmp_path):
    p = _write(tmp_path, "perms.yaml", "version: 1\ncommands:\n  - id: x\n    name: foo\n    permissions: [not_a_real_permission]\n")
    with pytest.raises(PolicyError, match="unknown permission"):
        perms_mod._compile(p)


def test_duplicate_entry_id_raises(tmp_path):
    p = _write(
        tmp_path, "perms.yaml",
        "version: 1\ncommands:\n"
        "  - {id: x, name: foo, permissions: []}\n"
        "  - {id: x, name: bar, permissions: []}\n",
    )
    with pytest.raises(PolicyError, match="duplicate command entry id"):
        perms_mod._compile(p)


def test_two_entries_claiming_same_command_name_raises(tmp_path):
    p = _write(
        tmp_path, "perms.yaml",
        "version: 1\ncommands:\n"
        "  - {id: a, name: foo, permissions: []}\n"
        "  - {id: b, name: foo, permissions: [file_read]}\n",
    )
    with pytest.raises(PolicyError, match="claimed by both"):
        perms_mod._compile(p)


def test_rule_with_no_condition_raises(tmp_path):
    p = _write(
        tmp_path, "perms.yaml",
        "version: 1\ncommands:\n"
        "  - id: x\n    name: foo\n    permissions: []\n    rules:\n      - {add: [file_write]}\n",
    )
    with pytest.raises(PolicyError, match="no condition"):
        perms_mod._compile(p)


def test_rule_with_no_add_raises(tmp_path):
    p = _write(
        tmp_path, "perms.yaml",
        "version: 1\ncommands:\n"
        "  - id: x\n    name: foo\n    permissions: []\n    rules:\n      - {any_flag: ['-x']}\n",
    )
    with pytest.raises(PolicyError, match="no 'add'"):
        perms_mod._compile(p)


def test_subcommand_default_without_subcommand_raises(tmp_path):
    p = _write(
        tmp_path, "perms.yaml",
        "version: 1\ncommands:\n"
        "  - {id: x, name: foo, permissions: [], subcommand_default: [untrusted_exec]}\n",
    )
    with pytest.raises(PolicyError, match="subcommand_default set without"):
        perms_mod._compile(p)


def test_malformed_yaml_raises(tmp_path):
    p = _write(tmp_path, "perms.yaml", "version: 1\ncommands: [\n")
    with pytest.raises(PolicyError, match="not valid YAML"):
        perms_mod._compile(p)


# ---------------------------------------------------------------------------
# extends: / disable: layering — same shape as policy.py's, own
# implementation (deliberately not sharing code with policy.py's allow/deny
# loader — different schema, see perms.py's module docstring).
# ---------------------------------------------------------------------------

def test_extends_adds_a_new_command(tmp_path):
    base = _write(tmp_path, "base.yaml", "version: 1\ncommands:\n  - {id: a, name: foo, permissions: []}\n")
    ext = _write(
        tmp_path, "ext.yaml",
        f"version: 1\nextends: {base.name}\ncommands:\n  - {{id: b, name: bar, permissions: [file_write]}}\n",
    )
    table = perms_mod._compile(ext)
    assert "foo" in table.by_name
    assert table.by_name["bar"].permissions == frozenset({Permission.FILE_WRITE})


def test_extends_same_id_replaces_base_entry(tmp_path):
    base = _write(tmp_path, "base.yaml", "version: 1\ncommands:\n  - {id: a, name: foo, permissions: []}\n")
    ext = _write(
        tmp_path, "ext.yaml",
        f"version: 1\nextends: {base.name}\ncommands:\n  - {{id: a, name: foo, permissions: [root]}}\n",
    )
    table = perms_mod._compile(ext)
    assert table.by_name["foo"].permissions == frozenset({Permission.ROOT})


def test_disable_drops_a_base_entry(tmp_path):
    base = _write(
        tmp_path, "base.yaml",
        "version: 1\ncommands:\n"
        "  - {id: a, name: foo, permissions: []}\n"
        "  - {id: b, name: bar, permissions: [file_write]}\n",
    )
    ext = _write(tmp_path, "ext.yaml", f"version: 1\nextends: {base.name}\ndisable: [a]\ncommands: []\n")
    table = perms_mod._compile(ext)
    assert "foo" not in table.by_name
    assert "bar" in table.by_name


def test_disable_unknown_id_raises(tmp_path):
    base = _write(tmp_path, "base.yaml", "version: 1\ncommands:\n  - {id: a, name: foo, permissions: []}\n")
    ext = _write(tmp_path, "ext.yaml", f"version: 1\nextends: {base.name}\ndisable: [nope]\ncommands: []\n")
    with pytest.raises(PolicyError, match="disable lists command entry"):
        perms_mod._compile(ext)


def test_disable_without_extends_raises(tmp_path):
    p = _write(tmp_path, "perms.yaml", "version: 1\ndisable: [a]\ncommands: []\n")
    with pytest.raises(PolicyError, match="'disable' requires 'extends'"):
        perms_mod._compile(p)


def test_builtin_perms_alias_resolves(tmp_path):
    ext = _write(tmp_path, "ext.yaml", "version: 1\nextends: builtin:perms\ncommands: []\n")
    table = perms_mod._compile(ext)
    assert "ls" in table.by_name  # from the shipped perms.yaml


def test_circular_extends_raises(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("version: 1\nextends: b.yaml\ncommands: []\n", encoding="utf-8")
    b.write_text("version: 1\nextends: a.yaml\ncommands: []\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="circular extends"):
        perms_mod._compile(a)


def test_worst_case_merges_with_base(tmp_path):
    base = _write(
        tmp_path, "base.yaml",
        "version: 1\ncommands: []\nworst_case:\n  foo: [file_write]\n",
    )
    ext = _write(
        tmp_path, "ext.yaml",
        f"version: 1\nextends: {base.name}\ncommands: []\nworst_case:\n  bar: [network_write]\n",
    )
    table = perms_mod._compile(ext)
    assert table.worst_case["foo"] == frozenset({Permission.FILE_WRITE})
    assert table.worst_case["bar"] == frozenset({Permission.NETWORK_WRITE})


# ---------------------------------------------------------------------------
# Fail-closed classify()/required_permissions_for_shell() when loading
# failed — every command needs ROOT until the file is fixed.
# ---------------------------------------------------------------------------

def test_classify_fails_closed_to_root_when_load_failed(monkeypatch):
    monkeypatch.setattr(perms_mod, "_load_error", "simulated broken perms.yaml")
    from TinyCTX.modules.shell.validate import _extract, _get_parser
    root = _get_parser().parse(b"echo hi").root_node
    cmd = _extract(root)[0]
    assert perms_mod.classify(cmd) == frozenset({Permission.ROOT})


def test_required_permissions_fails_closed_to_root_when_load_failed(monkeypatch):
    monkeypatch.setattr(perms_mod, "_load_error", "simulated broken perms.yaml")
    assert perms_mod.required_permissions_for_shell("echo hi") == {Permission.ROOT}


# ---------------------------------------------------------------------------
# _flag_is_known() — direct membership ONLY, deliberately no cluster
# decomposition. See its docstring: decomposition was tried first and is a
# real trap (find's "-delete" decomposes into the same letters as common,
# legitimately-registered short flags like "-d"/"-e"/"-l"/"-t").
# ---------------------------------------------------------------------------

def test_flag_is_known_direct_match():
    assert perms_mod._flag_is_known("-l", frozenset({"-l", "-a"})) is True
    assert perms_mod._flag_is_known("--foo", frozenset({"--foo"})) is True


def test_flag_is_known_no_cluster_decomposition():
    # "-la" is NOT known just because "-l" and "-a" both are — the raw
    # combined spelling has to be registered on its own.
    assert perms_mod._flag_is_known("-la", frozenset({"-l", "-a"})) is False


def test_flag_is_known_does_not_wave_through_spelled_out_options():
    # The regression case: -delete decomposes (character by character, same
    # as validate.py's own atom-building) into d/e/l/e/t/e — all of which
    # are individually completely ordinary, legitimately-registered short
    # flags elsewhere. Direct-match-only means none of that matters.
    known = frozenset({"-d", "-e", "-l", "-t"})
    assert perms_mod._flag_is_known("-delete", known) is False


def test_flag_is_known_unregistered_flag():
    assert perms_mod._flag_is_known("-z", frozenset({"-l", "-a"})) is False


# ---------------------------------------------------------------------------
# _unaccounted_flags() — the per-spec aggregation _eval_spec() uses.
# ---------------------------------------------------------------------------

def _spec(**kw):
    return perms_mod._CommandSpec(id="x", names=frozenset({"x"}), **kw)


def _cmd(flags):
    from TinyCTX.modules.shell.validate import Command
    return Command(name="x", atoms=frozenset(), flags=frozenset(flags),
                   operands=(), redirects=(), dynamic=False)


def test_unaccounted_flags_empty_when_all_known():
    spec = _spec(known_flags=frozenset({"-l", "-a"}))
    assert perms_mod._unaccounted_flags(spec, _cmd({"-l", "-a"})) == frozenset()


def test_unaccounted_flags_reports_unregistered():
    spec = _spec(known_flags=frozenset({"-l"}))
    assert perms_mod._unaccounted_flags(spec, _cmd({"-l", "-z"})) == frozenset({"-z"})


def test_unaccounted_flags_help_globally_exempt():
    spec = _spec()
    assert perms_mod._unaccounted_flags(spec, _cmd({"--help"})) == frozenset()


def test_unaccounted_flags_rule_referenced_flag_is_accounted_for():
    rule = perms_mod._Rule(any_flag=frozenset({"-X"}), add=frozenset({Permission.NETWORK_WRITE}))
    spec = _spec(rules=(rule,))
    assert perms_mod._unaccounted_flags(spec, _cmd({"-X"})) == frozenset()

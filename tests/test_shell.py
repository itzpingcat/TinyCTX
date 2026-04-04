"""
tests/test_shell.py

Tests for shell.py: command semantics and destructive command warnings.

Run with:
    python -m pytest tests/test_shell.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is importable
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from modules.filesystem.shell import (
    _extract_last_command,
    _interpret_exit_code,
    _normalize_windows_command,
    get_destructive_warning,
)


# ===================================================================
# Command semantics — exit code interpretation
# ===================================================================

class TestExtractLastCommand:
    def test_simple_command(self):
        assert _extract_last_command("grep foo bar") == "grep"

    def test_piped_command(self):
        assert _extract_last_command("cat file | grep pattern") == "grep"

    def test_multi_pipe(self):
        assert _extract_last_command("ps aux | grep python | wc -l") == "wc"

    def test_with_path(self):
        assert _extract_last_command("/usr/bin/grep foo") == "grep"

    def test_with_env_var(self):
        assert _extract_last_command("FOO=bar grep test") == "grep"

    def test_empty_command(self):
        assert _extract_last_command("") == ""


class TestCommandSemantics:
    def test_grep_no_matches_not_error(self):
        is_error, annotation = _interpret_exit_code("grep pattern file", 1)
        assert not is_error
        assert "no matches" in annotation

    def test_grep_real_error(self):
        is_error, annotation = _interpret_exit_code("grep pattern file", 2)
        assert is_error
        assert "exit 2" in annotation

    def test_grep_success(self):
        is_error, annotation = _interpret_exit_code("grep pattern file", 0)
        assert not is_error
        assert annotation == ""

    def test_rg_no_matches(self):
        is_error, annotation = _interpret_exit_code("rg pattern", 1)
        assert not is_error
        assert "no matches" in annotation

    def test_diff_files_differ(self):
        is_error, annotation = _interpret_exit_code("diff a.txt b.txt", 1)
        assert not is_error
        assert "differ" in annotation

    def test_diff_real_error(self):
        is_error, annotation = _interpret_exit_code("diff a.txt b.txt", 2)
        assert is_error

    def test_test_false_not_error(self):
        is_error, annotation = _interpret_exit_code("test -f noexist", 1)
        assert not is_error
        assert "false" in annotation

    def test_bracket_false_not_error(self):
        is_error, annotation = _interpret_exit_code("[ -f noexist ]", 1)
        assert not is_error
        assert "false" in annotation

    def test_find_partial_success(self):
        is_error, annotation = _interpret_exit_code("find / -name foo", 1)
        assert not is_error
        assert "inaccessible" in annotation

    def test_unknown_command_exit_1_is_error(self):
        is_error, annotation = _interpret_exit_code("python script.py", 1)
        assert is_error
        assert "exit 1" in annotation

    def test_piped_grep_semantics(self):
        """In a pipeline, the last command's semantics apply."""
        is_error, annotation = _interpret_exit_code("cat file | grep missing", 1)
        assert not is_error
        assert "no matches" in annotation


# ===================================================================
# Destructive command warnings
# ===================================================================

class TestDestructiveWarnings:
    def test_git_reset_hard(self):
        warning = get_destructive_warning("git reset --hard HEAD~3")
        assert warning is not None
        assert "uncommitted" in warning

    def test_git_push_force(self):
        warning = get_destructive_warning("git push --force origin main")
        assert warning is not None
        assert "remote" in warning

    def test_git_push_force_short(self):
        warning = get_destructive_warning("git push -f origin main")
        assert warning is not None

    def test_git_clean_f(self):
        warning = get_destructive_warning("git clean -fd")
        assert warning is not None
        assert "untracked" in warning

    def test_git_clean_dry_run_no_warning(self):
        warning = get_destructive_warning("git clean --dry-run -fd")
        assert warning is None

    def test_git_checkout_dot(self):
        warning = get_destructive_warning("git checkout -- .")
        assert warning is not None
        assert "discard" in warning

    def test_git_restore_dot(self):
        warning = get_destructive_warning("git restore -- .")
        assert warning is not None

    def test_git_stash_drop(self):
        warning = get_destructive_warning("git stash drop stash@{0}")
        assert warning is not None
        assert "stash" in warning

    def test_git_stash_clear(self):
        warning = get_destructive_warning("git stash clear")
        assert warning is not None

    def test_git_branch_force_delete(self):
        warning = get_destructive_warning("git branch -D feature-x")
        assert warning is not None
        assert "branch" in warning

    def test_git_no_verify(self):
        warning = get_destructive_warning("git commit --no-verify -m 'yolo'")
        assert warning is not None
        assert "hooks" in warning

    def test_git_amend(self):
        warning = get_destructive_warning("git commit --amend -m 'fix'")
        assert warning is not None
        assert "rewriting" in warning

    def test_rm_rf(self):
        warning = get_destructive_warning("rm -rf /tmp/stuff")
        assert warning is not None
        assert "recursively" in warning

    def test_rm_r(self):
        warning = get_destructive_warning("rm -r /tmp/stuff")
        assert warning is not None

    def test_drop_table(self):
        warning = get_destructive_warning("sqlite3 db.sqlite 'DROP TABLE users'")
        assert warning is not None
        assert "database" in warning

    def test_kubectl_delete(self):
        warning = get_destructive_warning("kubectl delete pod my-pod")
        assert warning is not None
        assert "Kubernetes" in warning

    def test_terraform_destroy(self):
        warning = get_destructive_warning("terraform destroy -auto-approve")
        assert warning is not None
        assert "Terraform" in warning

    def test_safe_command_no_warning(self):
        assert get_destructive_warning("git status") is None
        assert get_destructive_warning("ls -la") is None
        assert get_destructive_warning("git commit -m 'normal'") is None
        assert get_destructive_warning("git push origin main") is None
        assert get_destructive_warning("cat file.txt") is None


class TestWindowsNormalization:
    def test_non_windows_leaves_command_unchanged(self, monkeypatch):
        monkeypatch.setattr("modules.filesystem.shell._IS_WINDOWS", False)
        assert _normalize_windows_command("ls -la d:\\tq") == "ls -la d:\\tq"

    def test_ls_la_becomes_get_child_item_force(self, monkeypatch):
        monkeypatch.setattr("modules.filesystem.shell._IS_WINDOWS", True)
        assert (
            _normalize_windows_command(r"ls -la d:\tq")
            == "Get-ChildItem -Force -LiteralPath 'd:\\tq'"
        )

    def test_pwd_becomes_get_location(self, monkeypatch):
        monkeypatch.setattr("modules.filesystem.shell._IS_WINDOWS", True)
        assert _normalize_windows_command("pwd") == "Get-Location"

    def test_unsupported_ls_flags_are_left_alone(self, monkeypatch):
        monkeypatch.setattr("modules.filesystem.shell._IS_WINDOWS", True)
        assert _normalize_windows_command("ls -lah") == "ls -lah"

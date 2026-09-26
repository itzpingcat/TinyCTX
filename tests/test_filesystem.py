from pathlib import Path

from TinyCTX.modules.filesystem import _run_py_grep, _validate_glob_pattern


def test_python_grep_does_not_follow_file_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("outside-marker\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return

    result = _run_py_grep(
        "outside-marker",
        workspace,
        case_insensitive=False,
        include_glob=None,
        context_lines=0,
        output_mode="content",
        limit=20,
        allowed_roots=[workspace],
    )

    assert result == ""


def test_glob_rejects_parent_traversal():
    assert _validate_glob_pattern("../*.txt") is not None
    assert _validate_glob_pattern("nested/../../*.txt") is not None


def test_glob_accepts_in_root_pattern():
    assert _validate_glob_pattern("**/*.py") is None

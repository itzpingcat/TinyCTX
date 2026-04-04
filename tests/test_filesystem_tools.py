"""
tests/test_filesystem_tools.py

Tests for the filesystem module's new tools and guards:
  - replace_all on str_replace
  - grep (ripgrep wrapper / Python fallback)
  - glob_search
  - read-before-write guard
  - file staleness detection

Run with:
    python -m pytest tests/test_filesystem_tools.py -v
"""
from __future__ import annotations

import os
import time
import tempfile
import shutil
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers — register the filesystem module against a temp workspace
# ---------------------------------------------------------------------------

class _MockConfig:
    def __init__(self, ws_path: str):
        self.workspace = type("WS", (), {"path": ws_path})()
        self.shell_timeout = 60


class _MockToolHandler:
    def __init__(self):
        self.tools: dict = {}

    def register_tool(self, func, always_on=False):
        self.tools[func.__name__] = func


class _MockContext:
    def __init__(self):
        self.prompts: dict = {}

    def register_prompt(self, pid, provider, *, role="system", priority=0):
        self.prompts[pid] = {
            "provider": provider,
            "role": role,
            "priority": priority,
        }


class _MockAgent:
    def __init__(self, ws_path: str):
        self.config = _MockConfig(ws_path)
        self.tool_handler = _MockToolHandler()
        self.context = _MockContext()


@pytest.fixture
def workspace(tmp_path):
    """Create a temp workspace with some test files."""
    # Create files for testing
    (tmp_path / "hello.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (tmp_path / "greet.py").write_text("def greet(name):\n    return f'hi {name}'\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("# Project\nSome readme text.\n", encoding="utf-8")

    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "main.py").write_text("import hello\nhello.hello()\n", encoding="utf-8")
    (sub / "utils.ts").write_text("export function add(a: number, b: number) { return a + b; }\n", encoding="utf-8")

    # .git dir — should be excluded from search
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="utf-8")

    return tmp_path


@pytest.fixture
def tools(workspace):
    """Register the filesystem module and return the tools dict."""
    import sys
    # Ensure project root is on path so 'contracts' and 'modules' are importable
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from modules.filesystem.__main__ import register

    agent = _MockAgent(str(workspace))
    register(agent)
    return agent.tool_handler.tools


@pytest.fixture
def filesystem_agent(workspace):
    """Register the filesystem module and return the mock agent."""
    import sys
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from modules.filesystem.__main__ import register

    agent = _MockAgent(str(workspace))
    register(agent)
    return agent


# ===================================================================
# str_replace — replace_all flag
# ===================================================================

class TestStrReplaceAll:
    def test_single_match_works(self, tools, workspace):
        tools["view"](path="hello.py")
        result = tools["str_replace"](path="hello.py", old_str="world", new_str="earth")
        assert "replaced 1 occurrence" in result
        content = (workspace / "hello.py").read_text(encoding="utf-8")
        assert "earth" in content

    def test_multiple_matches_blocked_by_default(self, tools, workspace):
        # Write a file with repeated content
        (workspace / "dup.txt").write_text("aaa bbb aaa ccc aaa", encoding="utf-8")
        tools["view"](path="dup.txt")
        result = tools["str_replace"](path="dup.txt", old_str="aaa")
        assert "appears 3 times" in result
        assert "replace_all" in result

    def test_replace_all_replaces_every_occurrence(self, tools, workspace):
        (workspace / "dup.txt").write_text("aaa bbb aaa ccc aaa", encoding="utf-8")
        tools["view"](path="dup.txt")
        result = tools["str_replace"](path="dup.txt", old_str="aaa", new_str="zzz", replace_all=True)
        assert "replaced 3 occurrences" in result
        content = (workspace / "dup.txt").read_text(encoding="utf-8")
        assert content == "zzz bbb zzz ccc zzz"

    def test_replace_all_with_single_match(self, tools, workspace):
        tools["view"](path="hello.py")
        result = tools["str_replace"](path="hello.py", old_str="world", new_str="earth", replace_all=True)
        assert "replaced 1 occurrence" in result

    def test_replace_all_not_found(self, tools, workspace):
        tools["view"](path="hello.py")
        result = tools["str_replace"](path="hello.py", old_str="zzznomatch")
        assert "not found" in result


# ===================================================================
# grep — search functionality
# ===================================================================

class TestGrep:
    def test_files_mode_default(self, tools):
        result = tools["grep"](pattern="def ")
        assert "hello.py" in result
        assert "greet.py" in result

    def test_files_mode_excludes_git(self, tools):
        result = tools["grep"](pattern="core")
        # .git/config contains "core" but should be excluded
        assert ".git" not in result

    def test_content_mode(self, tools):
        result = tools["grep"](pattern="def hello", output_mode="content")
        assert "def hello" in result
        assert "hello.py" in result

    def test_count_mode(self, tools):
        result = tools["grep"](pattern="import", output_mode="count")
        assert "matches" in result.lower() or "1" in result

    def test_no_matches(self, tools):
        result = tools["grep"](pattern="zzznomatchzzz")
        assert "no matches" in result.lower()

    def test_include_filter(self, tools):
        result = tools["grep"](pattern="def", include="*.py")
        assert "hello.py" in result
        # .ts file should not appear
        assert "utils.ts" not in result

    def test_case_insensitive(self, tools):
        result = tools["grep"](pattern="PROJECT", case_insensitive=True)
        assert "readme.md" in result

    def test_context_lines(self, tools):
        # Search a directory so rg doesn't need special single-file handling
        result = tools["grep"](pattern="return", include="hello.py", output_mode="content", context_lines=1)
        # Should include the match line at minimum
        assert "return" in result

    def test_bad_path(self, tools):
        result = tools["grep"](pattern="test", path="nonexistent_dir")
        assert "error" in result.lower()

    def test_limit_caps_results(self, tools):
        result = tools["grep"](pattern=".", output_mode="content", limit=3)
        lines = [l for l in result.splitlines() if l.strip()]
        # Should be capped (may include truncation notice)
        assert len(lines) <= 5  # 3 + possible header/footer

    def test_subdirectory_search(self, tools):
        result = tools["grep"](pattern="import", path="src")
        assert "main.py" in result


# ===================================================================
# glob_search — file finding
# ===================================================================

class TestGlobSearch:
    def test_find_python_files(self, tools):
        result = tools["glob_search"](pattern="**/*.py")
        assert "hello.py" in result
        assert "greet.py" in result
        assert "main.py" in result

    def test_find_markdown_files(self, tools):
        result = tools["glob_search"](pattern="*.md")
        assert "readme.md" in result

    def test_excludes_git_dir(self, tools):
        result = tools["glob_search"](pattern="**/*")
        assert ".git" not in result

    def test_no_files_found(self, tools):
        result = tools["glob_search"](pattern="*.zzz")
        assert "no files found" in result.lower()

    def test_bad_path(self, tools):
        result = tools["glob_search"](pattern="*.py", path="nonexistent")
        assert "error" in result.lower()

    def test_subdirectory_search(self, tools):
        result = tools["glob_search"](pattern="*.py", path="src")
        assert "main.py" in result
        # Should not include files from parent
        assert "hello.py" not in result

    def test_limit(self, tools):
        result = tools["glob_search"](pattern="**/*", limit=2)
        lines = result.strip().splitlines()
        # Header line + at most 2 file lines
        file_lines = [l for l in lines if not l.startswith("[")]
        assert len(file_lines) <= 2

    def test_sorted_by_mtime(self, tools, workspace):
        # Touch hello.py to make it newest
        time.sleep(0.05)
        (workspace / "hello.py").write_text("# touched\n", encoding="utf-8")
        result = tools["glob_search"](pattern="*.py")
        lines = [l for l in result.strip().splitlines() if not l.startswith("[")]
        # hello.py should be first (newest)
        assert lines[0] == "hello.py"


class TestFilesystemPrompt:
    def test_prompt_includes_workspace_and_source_root(self, filesystem_agent, workspace):
        prompt_info = filesystem_agent.context.prompts["filesystem_tools"]
        prompt = prompt_info["provider"](None)

        assert str(workspace) in prompt
        assert str(Path.cwd().resolve()) in prompt
        assert "Do not waste tool calls rediscovering the repo path" in prompt
        assert "Prefer view(), grep(), and glob_search()" in prompt


# ===================================================================
# Read-before-write guard
# ===================================================================

class TestReadBeforeWrite:
    def test_str_replace_blocked_without_read(self, tools, workspace):
        result = tools["str_replace"](path="hello.py", old_str="world", new_str="earth")
        assert "not been read" in result
        # File should be unchanged
        assert "world" in (workspace / "hello.py").read_text(encoding="utf-8")

    def test_str_replace_allowed_after_read(self, tools, workspace):
        tools["view"](path="hello.py")
        result = tools["str_replace"](path="hello.py", old_str="world", new_str="earth")
        assert "replaced" in result

    def test_write_file_blocked_for_existing_without_read(self, tools, workspace):
        result = tools["write_file"](path="hello.py", content="overwritten!", mode="overwrite")
        assert "not been read" in result
        # File should be unchanged
        assert "def hello" in (workspace / "hello.py").read_text(encoding="utf-8")

    def test_write_file_allowed_for_new_file(self, tools, workspace):
        result = tools["write_file"](path="brand_new.txt", content="new content", mode="overwrite")
        assert "created" in result
        assert (workspace / "brand_new.txt").read_text(encoding="utf-8") == "new content"

    def test_write_after_write_works(self, tools, workspace):
        """After write_file creates a file, a second write should work (mtime tracked)."""
        tools["write_file"](path="new.txt", content="first", mode="overwrite")
        result = tools["write_file"](path="new.txt", content="second", mode="overwrite")
        assert "overwrote" in result
        assert (workspace / "new.txt").read_text(encoding="utf-8") == "second"

    def test_str_replace_after_str_replace_works(self, tools, workspace):
        """Back-to-back edits should work (mtime updated after each write)."""
        tools["view"](path="hello.py")
        tools["str_replace"](path="hello.py", old_str="world", new_str="earth")
        result = tools["str_replace"](path="hello.py", old_str="earth", new_str="mars")
        assert "replaced" in result


# ===================================================================
# File staleness detection
# ===================================================================

class TestStalenessDetection:
    def test_external_modification_blocks_write(self, tools, workspace):
        tools["view"](path="hello.py")
        # Simulate external modification
        time.sleep(0.05)
        (workspace / "hello.py").write_text("# externally changed\n", encoding="utf-8")
        result = tools["str_replace"](path="hello.py", old_str="externally", new_str="changed")
        assert "modified since" in result

    def test_re_read_clears_staleness(self, tools, workspace):
        tools["view"](path="hello.py")
        time.sleep(0.05)
        (workspace / "hello.py").write_text("# externally changed\n", encoding="utf-8")
        # First attempt blocked
        result = tools["str_replace"](path="hello.py", old_str="externally", new_str="internally")
        assert "modified since" in result
        # Re-read
        tools["view"](path="hello.py")
        # Second attempt succeeds
        result = tools["str_replace"](path="hello.py", old_str="externally", new_str="internally")
        assert "replaced" in result

    def test_write_file_staleness(self, tools, workspace):
        tools["view"](path="readme.md")
        time.sleep(0.05)
        (workspace / "readme.md").write_text("# changed externally\n", encoding="utf-8")
        result = tools["write_file"](path="readme.md", content="overwrite", mode="overwrite")
        assert "modified since" in result


# ===================================================================
# Quote normalization — curly ↔ straight quote matching
# ===================================================================

class TestQuoteNormalization:
    def test_straight_quotes_match_curly_in_file(self, tools, workspace):
        """LLM sends straight quotes, file has curly quotes — should match."""
        (workspace / "quotes.txt").write_text(
            "She said \u201CHello\u201D and \u2018goodbye\u2019\n", encoding="utf-8"
        )
        tools["view"](path="quotes.txt")
        result = tools["str_replace"](
            path="quotes.txt",
            old_str='She said "Hello"',
            new_str='She said "Hi"',
        )
        assert "replaced" in result
        content = (workspace / "quotes.txt").read_text(encoding="utf-8")
        # The replacement should have happened
        assert "Hi" in content

    def test_curly_quotes_match_straight_in_file(self, tools, workspace):
        """LLM sends curly quotes, file has straight quotes — should match."""
        (workspace / "straight.txt").write_text(
            'He said "yes" and \'no\'\n', encoding="utf-8"
        )
        tools["view"](path="straight.txt")
        result = tools["str_replace"](
            path="straight.txt",
            old_str='He said \u201Cyes\u201D',
            new_str='He said "maybe"',
        )
        assert "replaced" in result

    def test_exact_match_preferred_over_normalized(self, tools, workspace):
        """When exact match exists, don't use normalized matching."""
        (workspace / "exact.txt").write_text(
            'say "hello" world\n', encoding="utf-8"
        )
        tools["view"](path="exact.txt")
        result = tools["str_replace"](
            path="exact.txt",
            old_str='"hello"',
            new_str='"hi"',
        )
        assert "replaced" in result
        content = (workspace / "exact.txt").read_text(encoding="utf-8")
        assert '"hi"' in content

    def test_no_match_still_fails(self, tools, workspace):
        """Normalization doesn't create false matches."""
        tools["view"](path="hello.py")
        result = tools["str_replace"](path="hello.py", old_str="zzznomatch")
        assert "not found" in result


# ===================================================================
# Trailing whitespace stripping on new_str
# ===================================================================

class TestTrailingWhitespace:
    def test_trailing_spaces_stripped_from_replacement(self, tools, workspace):
        (workspace / "ws.txt").write_text("line1\nline2\n", encoding="utf-8")
        tools["view"](path="ws.txt")
        result = tools["str_replace"](
            path="ws.txt",
            old_str="line1",
            new_str="replaced   ",  # trailing spaces
        )
        assert "replaced" in result
        content = (workspace / "ws.txt").read_text(encoding="utf-8")
        # Trailing spaces should be stripped
        assert "replaced   " not in content
        assert "replaced\n" in content

    def test_multiline_trailing_ws_stripped(self, tools, workspace):
        (workspace / "multi.txt").write_text("aaa\nbbb\n", encoding="utf-8")
        tools["view"](path="multi.txt")
        result = tools["str_replace"](
            path="multi.txt",
            old_str="aaa\nbbb",
            new_str="xxx   \nyyy  ",
        )
        assert "replaced" in result
        content = (workspace / "multi.txt").read_text(encoding="utf-8")
        assert "xxx\nyyy\n" in content

    def test_intentional_content_preserved(self, tools, workspace):
        """Non-trailing whitespace is preserved."""
        (workspace / "indent.txt").write_text("def foo():\n    pass\n", encoding="utf-8")
        tools["view"](path="indent.txt")
        result = tools["str_replace"](
            path="indent.txt",
            old_str="    pass",
            new_str="    return 42",
        )
        assert "replaced" in result
        content = (workspace / "indent.txt").read_text(encoding="utf-8")
        assert "    return 42" in content


# ===================================================================
# Unchanged file detection — view() returns stub on re-read
# ===================================================================

class TestUnchangedDetection:
    def test_second_read_returns_stub(self, tools, workspace):
        """Reading the same file twice without changes returns a short stub."""
        result1 = tools["view"](path="hello.py")
        assert "def hello" in result1  # full content
        result2 = tools["view"](path="hello.py")
        assert "unchanged" in result2
        assert "def hello" not in result2  # no full content

    def test_stub_includes_line_count(self, tools, workspace):
        result1 = tools["view"](path="hello.py")
        result2 = tools["view"](path="hello.py")
        assert "2 lines" in result2  # hello.py has 2 lines

    def test_modified_file_not_stubbed(self, tools, workspace):
        """If the file changes between reads, return full content."""
        tools["view"](path="hello.py")
        time.sleep(0.05)
        (workspace / "hello.py").write_text("# changed\n", encoding="utf-8")
        result = tools["view"](path="hello.py")
        assert "changed" in result
        assert "unchanged" not in result

    def test_different_range_not_stubbed(self, tools, workspace):
        """Different view_range should return full content."""
        tools["view"](path="hello.py")  # full read
        result = tools["view"](path="hello.py", view_range=[1, 1])  # partial read
        assert "unchanged" not in result

    def test_same_range_returns_stub(self, tools, workspace):
        """Same view_range on unchanged file returns stub."""
        tools["view"](path="hello.py", view_range=[1, 1])
        result = tools["view"](path="hello.py", view_range=[1, 1])
        assert "unchanged" in result

    def test_write_clears_stub(self, tools, workspace):
        """After str_replace, next view() returns full content (not stub)."""
        tools["view"](path="hello.py")
        tools["str_replace"](path="hello.py", old_str="world", new_str="earth")
        result = tools["view"](path="hello.py")
        assert "earth" in result
        assert "unchanged" not in result

    def test_write_file_clears_stub(self, tools, workspace):
        """After write_file, next view() returns full content."""
        tools["view"](path="hello.py")
        tools["write_file"](path="hello.py", content="# new\n", mode="overwrite")
        result = tools["view"](path="hello.py")
        assert "new" in result
        assert "unchanged" not in result

    def test_directory_listing_not_affected(self, tools, workspace):
        """Directory listing should never return a stub."""
        result1 = tools["view"](path=".")
        result2 = tools["view"](path=".")
        # Both should be full listings
        assert "hello.py" in result1
        assert "hello.py" in result2
        assert "unchanged" not in result2

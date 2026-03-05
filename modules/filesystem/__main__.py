"""
modules/filesystem/__main__.py

Registers filesystem tools (shell, view, create_file, str_replace) into the
agent loop's tool_handler. No imports from utils or contracts — everything
arrives through the agent_loop argument.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def register(agent_loop) -> None:
    workspace = Path(agent_loop.config.memory.workspace_path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    def resolve(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else workspace / p

    def shell(command: str) -> str:
        """Run a shell command in the workspace. Returns stdout, stderr, and exit code.

        Args:
            command: The shell command to run.
        """
        try:
            result = subprocess.run(
                command, shell=True, cwd=workspace,
                capture_output=True, text=True, timeout=60,
            )
            parts = []
            if result.stdout: parts.append(result.stdout.rstrip())
            if result.stderr: parts.append(f"[stderr]\n{result.stderr.rstrip()}")
            if result.returncode != 0: parts.append(f"[exit {result.returncode}]")
            return "\n".join(parts) if parts else "[no output]"
        except subprocess.TimeoutExpired:
            return "[error: timed out after 60s]"
        except Exception as e:
            return f"[error: {e}]"

    def view(path: str, view_range: list = None) -> str:
        """Read a file with line numbers, or list a directory.

        Args:
            path: File or directory path.
            view_range: [start_line, end_line]. Use -1 for end_line to read to EOF.
        """
        p = resolve(path)
        if not p.exists():
            return f"[error: not found: {p}]"
        if p.is_dir():
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
            if not entries:
                return f"[empty directory: {p}]"
            return f"[{p}]\n" + "\n".join(f"  {e.name}{'/' if e.is_dir() else ''}" for e in entries)
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "[error: binary file, cannot read as text]"
        lines = text.splitlines()
        total = len(lines)
        if view_range:
            start = max(1, int(view_range[0]))
            end   = min(int(view_range[1]) if view_range[1] != -1 else total, total)
            return f"[{p} | lines {start}–{end} of {total}]\n" + "\n".join(
                f"{i:>6}\t{l}" for i, l in enumerate(lines[start-1:end], start)
            )
        return f"[{p} | {total} lines]\n" + "\n".join(
            f"{i:>6}\t{l}" for i, l in enumerate(lines, 1)
        )

    def create_file(path: str, content: str) -> str:
        """Create a new file with content. Fails if the file already exists — use str_replace to edit existing files.

        Args:
            path: Path to the new file.
            content: Full file content.
        """
        p = resolve(path)
        if p.exists():
            return f"[error: file already exists: {p} — use str_replace to edit it]"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[created {p} ({len(content)} chars)]"

    def str_replace(path: str, old_str: str, new_str: str = "") -> str:
        """Replace a unique string in an existing file. old_str must appear exactly once.

        Args:
            path: Path to the file.
            old_str: Exact string to replace. Must be unique in the file.
            new_str: Replacement string. Leave empty to delete old_str.
        """
        p = resolve(path)
        if not p.exists():
            return f"[error: file not found: {p}]"
        original = p.read_text(encoding="utf-8")
        count = original.count(old_str)
        if count == 0:
            return f"[error: old_str not found in {p}]"
        if count > 1:
            return f"[error: old_str appears {count} times — add more context to make it unique]"
        p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
        return f"[replaced 1 occurrence in {p}]"

    agent_loop.tool_handler.register_tool(shell)
    agent_loop.tool_handler.register_tool(view)
    agent_loop.tool_handler.register_tool(create_file)
    agent_loop.tool_handler.register_tool(str_replace)
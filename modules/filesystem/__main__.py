"""
Filesystem extension — __main__.py

Four tools mirroring Claude's own filesystem tool suite:

  bash        — run a shell command, get stdout/stderr/returncode
  view        — read a file (with optional line range) or list a directory.
                if a file handler is registered for the file type, it runs
                automatically and returns converted content instead of raw bytes.
  create_file — create a new file with content (fails if file exists)
  str_replace  — replace a unique string in an existing file

File handlers from other modules (pdf, image captioner, etc.) register
themselves into the Registry. view looks them up transparently — the LLM
never needs to think about conversion.
"""
from __future__ import annotations
import subprocess
import mimetypes
from pathlib import Path


def register_global(registry, config):
    workspace  = config.get("workspace", "/home/agent")
    cache_size = config.get("cache_size", 128)
    workspace = Path(config.get("workspace", Path.home() / ".tinyctx")).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # ------------------------------------------------------------------
    # Conversion cache: (resolved_path_str, mtime, size) -> str
    # ------------------------------------------------------------------
    _cache: dict[tuple, str] = {}
    _cache_order: list[tuple] = []

    def _cache_get(key):
        return _cache.get(key)

    def _cache_set(key, value):
        if len(_cache_order) >= cache_size:
            evict = _cache_order.pop(0)
            _cache.pop(evict, None)
        _cache[key] = value
        _cache_order.append(key)

    def _convert_if_needed(path: Path) -> str | None:
        """
        If a file handler is registered for this file type, run it and
        return the converted string (cached by mtime+size).
        Returns None if no handler is registered — caller reads raw text.
        """
        mime, _ = mimetypes.guess_type(str(path))
        handler  = registry.get_file_handler(str(path), mime)
        if not handler:
            return None

        stat = path.stat()
        key  = (str(path), stat.st_mtime, stat.st_size)

        cached = _cache_get(key)
        if cached is not None:
            return cached

        result = handler(path.read_bytes(), str(path))
        _cache_set(key, result)
        return result

    def _resolve(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else Path(workspace) / p

    # ------------------------------------------------------------------
    # bash
    # ------------------------------------------------------------------

    def handle_shell(args: dict) -> str:
        command = args.get("command", "").strip()
        if not command:
            return "[error: 'command' is required]"

        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-Command",
                    command,
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=60,
            )

            parts = []
            if result.stdout:
                parts.append(result.stdout.rstrip())
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr.rstrip()}")
            if result.returncode != 0:
                parts.append(f"[exit code: {result.returncode}]")

            return "\n".join(parts) if parts else "[no output]"

        except subprocess.TimeoutExpired:
            return "[error: command timed out after 60s]"
        except Exception as e:
            return f"[error: {e}]"

    registry.register_tool(
        name="shell",
        schema={
            "name": "shell",
            "description": (
                "Run a powershell command in the workspace. "
                "Returns stdout, stderr, and exit code if non-zero. "
                "Use for file operations, running scripts, installing packages, "
                "listing directories, grepping, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type":        "string",
                        "description": "The powershell command to run.",
                    },
                },
                "required": ["command"],
            },
        },
        handler=handle_shell,
    )

    # ------------------------------------------------------------------
    # view
    # ------------------------------------------------------------------

    def handle_view(args: dict) -> str:
        raw_path   = args.get("path", "").strip()
        view_range = args.get("view_range")   # [start_line, end_line] or null

        if not raw_path:
            return "[error: 'path' is required]"

        path = _resolve(raw_path)

        if not path.exists():
            return f"[error: path not found: {path}]"

        # directory listing
        if path.is_dir():
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            if not entries:
                return f"[empty directory: {path}]"
            lines = [f"[{path}]"]
            for e in entries:
                lines.append(f"  {'  ' if e.is_file() else ''}{e.name}{'/' if e.is_dir() else ''}")
            return "\n".join(lines)

        # file — try conversion first
        converted = _convert_if_needed(path)

        if converted is not None:
            text = converted
        else:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return f"[binary file — no handler registered for {path.suffix or 'this type'}]"

        lines = text.splitlines()
        total = len(lines)

        if view_range:
            start = max(1, int(view_range[0]))
            end   = int(view_range[1]) if view_range[1] != -1 else total
            end   = min(end, total)
            selected = lines[start - 1:end]
            header = f"[{path} | lines {start}–{end} of {total}]\n"
            return header + "\n".join(
                f"{i:>6}\t{l}" for i, l in enumerate(selected, start=start)
            )

        # full file with line numbers
        header = f"[{path} | {total} lines]\n"
        return header + "\n".join(f"{i:>6}\t{l}" for i, l in enumerate(lines, start=1))

    registry.register_tool(
        name="view",
        schema={
            "name":        "view",
            "description": (
                "Read a file with line numbers, or list a directory. "
                "For files with registered handlers (PDF, images, etc.), "
                "content is automatically converted to readable text. "
                "Use view_range to read a specific range of lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type":        "string",
                        "description": "File or directory path.",
                    },
                    "view_range": {
                        "type":        "array",
                        "items":       {"type": "integer"},
                        "minItems":    2,
                        "maxItems":    2,
                        "description": "[start_line, end_line]. Use -1 for end_line to read to EOF.",
                    },
                },
                "required": ["path"],
            },
        },
        handler=handle_view,
    )

    # ------------------------------------------------------------------
    # create_file
    # ------------------------------------------------------------------

    def handle_create_file(args: dict) -> str:
        raw_path = args.get("path", "").strip()
        content  = args.get("content", "")

        if not raw_path:
            return "[error: 'path' is required]"

        path = _resolve(raw_path)

        if path.exists():
            return f"[error: file already exists: {path}. use str_replace to edit it]"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"[created {path} ({len(content)} chars)]"

    registry.register_tool(
        name="create_file",
        schema={
            "name":        "create_file",
            "description": (
                "Create a new file with content. "
                "Fails if the file already exists — use str_replace to edit existing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Path to the new file."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
        handler=handle_create_file,
    )

    # ------------------------------------------------------------------
    # str_replace
    # ------------------------------------------------------------------

    def handle_str_replace(args: dict) -> str:
        raw_path = args.get("path", "").strip()
        old_str  = args.get("old_str", "")
        new_str  = args.get("new_str", "")

        if not raw_path:
            return "[error: 'path' is required]"
        if not old_str:
            return "[error: 'old_str' is required]"

        path = _resolve(raw_path)

        if not path.exists():
            return f"[error: file not found: {path}]"

        original = path.read_text(encoding="utf-8")
        count    = original.count(old_str)

        if count == 0:
            return f"[error: old_str not found in {path}]"
        if count > 1:
            return (
                f"[error: old_str appears {count} times in {path} — "
                f"it must be unique. add more context to make it unique]"
            )

        updated = original.replace(old_str, new_str, 1)
        path.write_text(updated, encoding="utf-8")
        return f"[replaced 1 occurrence in {path}]"

    registry.register_tool(
        name="str_replace",
        schema={
            "name":        "str_replace",
            "description": (
                "Replace a unique string in an existing file. "
                "old_str must appear exactly once — if it appears multiple times, "
                "add more surrounding context to make it unique."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Path to the file to edit."},
                    "old_str": {"type": "string", "description": "The exact string to replace. Must be unique in the file."},
                    "new_str": {"type": "string", "description": "The replacement string. Empty string to delete old_str."},
                },
                "required": ["path", "old_str"],
            },
        },
        handler=handle_str_replace,
    )


def register(context, config):
    pass  # no per-session context hooks needed
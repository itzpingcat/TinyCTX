"""
modules/filesystem/__main__.py

Registers filesystem tools (shell, view, write_file, str_replace, grep, glob)
into the agent loop's tool_handler. No imports from utils or contracts —
everything arrives through the agent argument.

Shell execution is delegated to shell.py, which handles platform detection,
blacklist enforcement, and subprocess dispatch.
"""
from __future__ import annotations

import base64
import fnmatch
import logging
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path

from contracts import IMAGE_BLOCK_PREFIX
from modules.filesystem.shell import load_blacklist, run_command

logger = logging.getLogger(__name__)

# Image MIME types we can pass to a vision model as an image_url block.
_VISION_MIMES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})

# Extension fallback map for cases where mimetypes guesses wrong.
_EXT_TO_MIME: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


def _image_mime(path: Path) -> str | None:
    """Return the vision-compatible MIME type for a file, or None if not an image."""
    ext = path.suffix.lower()
    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime in _VISION_MIMES:
        return mime
    return None


def register(agent) -> None:
    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    shell_timeout: int = getattr(agent.config, 'shell_timeout', 60)

    # Load blacklist once at register time. Restart to pick up edits.
    blacklist = load_blacklist()

    def resolve(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else workspace / p

    def shell(command: str) -> str:
        """Run a shell command in the workspace. Returns stdout, stderr, and exit code.
        On Linux/macOS runs via bash. On Windows runs via PowerShell.
        Blocked commands return an error string without executing.

        Args:
            command: The shell command to run.
        """
        return run_command(command, cwd=workspace, timeout=shell_timeout, blacklist=blacklist)

    def view(path: str, view_range: list = None) -> str:
        """Read a file with line numbers, list a directory, or display an image.

        For image files (jpg, png, gif, webp) the raw image bytes are returned
        as a vision content block so the model can see the image directly.
        For all other binary files an error is returned.

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

        # --- image handling ---
        mime = _image_mime(p)
        if mime:
            try:
                raw = p.read_bytes()
            except OSError as exc:
                return f"[error: could not read {p}: {exc}]"
            b64 = base64.b64encode(raw).decode()
            # Return a sentinel that agent._execute_tool knows how to unwrap.
            return f"{IMAGE_BLOCK_PREFIX}{mime};{b64}"

        # --- text handling ---
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "[error: binary file, cannot read as text]"
        lines = text.splitlines()
        total = len(lines)
        if view_range:
            try:
                # If the agent sent a string like "1,20", convert it to a list [1, 20]
                if isinstance(view_range, str):
                    parts = []
                    for chunk in view_range.split(','):
                        chunk = chunk.strip()
                        if '-' in chunk:
                            start, end = chunk.split('-', 1)
                            parts.extend([start.strip(), end.strip()])
                        else:
                            parts.append(chunk)
                    view_range = parts
                start = int(view_range[0]) - 1
                end   = int(view_range[1]) if int(view_range[1]) != -1 else total
                lines = lines[start:end]
            except (ValueError, IndexError):
                return "[error: view_range must be [start, end] or 'start,end' integers]"

        return f"[{p} | {total} lines]\n" + "\n".join(
            f"{i:>6}\t{l}" for i, l in enumerate(lines, 1)
        )

    def write_file(path: str, content: str = "", mode: str = "append") -> str:
        """Write content to a file. Creates the file and any missing parent directories if they don't exist.

        Args:
            path: Path to the file.
            content: Content to write. Omit or pass empty string to create/truncate to empty.
            mode: How to write the content.
                  'append'    — add content after existing content (default).
                  'prepend'   — insert content before existing content.
                  'overwrite' — replace the entire file with content.
        """
        p = resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()

        if mode == "overwrite" or not existed:
            p.write_text(content, encoding="utf-8")
            action = "truncated" if existed and content == "" else ("overwrote" if existed else "created")
        elif mode == "prepend":
            existing = p.read_text(encoding="utf-8")
            p.write_text(content + existing, encoding="utf-8")
            action = "prepended"
        else:  # append (default)
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
            action = "appended"

        return f"[{action} {p} ({len(content)} chars)]"

    def str_replace(path: str, old_str: str, new_str: str = "", replace_all: bool = False) -> str:
        """Replace a string in an existing file. By default old_str must appear exactly once.

        Args:
            path: Path to the file.
            old_str: Exact string to replace. Must be unique unless replace_all is true.
            new_str: Replacement string. Leave empty to delete old_str.
            replace_all: If true, replace every occurrence instead of requiring uniqueness.
        """
        p = resolve(path)
        if not p.exists():
            return f"[error: file not found: {p}]"
        original = p.read_text(encoding="utf-8")
        count = original.count(old_str)
        if count == 0:
            return f"[error: old_str not found in {p}]"
        if count > 1 and not replace_all:
            return f"[error: old_str appears {count} times — add more context to make it unique, or set replace_all=true]"
        if replace_all:
            p.write_text(original.replace(old_str, new_str), encoding="utf-8")
            return f"[replaced {count} occurrences in {p}]"
        p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
        return f"[replaced 1 occurrence in {p}]"

    # ------------------------------------------------------------------
    # grep — ripgrep wrapper with Python fallback
    # ------------------------------------------------------------------

    # Directories always excluded from grep results (VCS metadata).
    _VCS_DIRS = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}

    # Default result cap — prevents context blowup on broad patterns.
    _GREP_DEFAULT_LIMIT = 200

    _has_rg = shutil.which("rg") is not None

    def _run_rg(
        pattern: str,
        search_path: Path,
        *,
        case_insensitive: bool,
        include_glob: str | None,
        file_type: str | None,
        context_lines: int,
        output_mode: str,
        limit: int,
    ) -> str:
        """Run ripgrep and return raw stdout."""
        args = ["rg", "--hidden", "--max-columns", "500"]
        for d in _VCS_DIRS:
            args += ["--glob", f"!{d}"]
        if case_insensitive:
            args.append("-i")
        if include_glob:
            for g in include_glob.split(","):
                g = g.strip()
                if g:
                    args += ["--glob", g]
        if file_type:
            args += ["--type", file_type]
        if output_mode == "files":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
        else:
            args.append("-n")
            if context_lines > 0:
                args += ["-C", str(context_lines)]
        if pattern.startswith("-"):
            args += ["-e", pattern]
        else:
            args.append(pattern)
        args.append(str(search_path))
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            return result.stdout.rstrip()
        except FileNotFoundError:
            return "[error: ripgrep not found]"
        except subprocess.TimeoutExpired:
            return "[error: grep timed out after 30s]"

    def _run_py_grep(
        pattern: str,
        search_path: Path,
        *,
        case_insensitive: bool,
        include_glob: str | None,
        context_lines: int,
        output_mode: str,
        limit: int,
    ) -> str:
        """Pure-Python fallback when rg is not installed."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return f"[error: invalid regex — {exc}]"

        globs = []
        if include_glob:
            globs = [g.strip() for g in include_glob.split(",") if g.strip()]

        matches: list[str] = []
        file_hits: list[str] = []
        count_map: dict[str, int] = {}

        for root, dirs, files in os.walk(search_path):
            # Prune VCS dirs
            dirs[:] = [d for d in dirs if d not in _VCS_DIRS]
            for fname in files:
                if globs and not any(fnmatch.fnmatch(fname, g) for g in globs):
                    continue
                fpath = Path(root) / fname
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeDecodeError):
                    continue
                lines = text.splitlines()
                hit_indices = [i for i, ln in enumerate(lines) if regex.search(ln)]
                if not hit_indices:
                    continue
                rel = fpath.relative_to(search_path)
                if output_mode == "files":
                    file_hits.append(str(rel))
                    if len(file_hits) >= limit:
                        break
                elif output_mode == "count":
                    count_map[str(rel)] = len(hit_indices)
                else:
                    for idx in hit_indices:
                        start = max(0, idx - context_lines)
                        end = min(len(lines), idx + context_lines + 1)
                        for li in range(start, end):
                            matches.append(f"{rel}:{li + 1}:{lines[li]}")
                        if len(matches) >= limit:
                            break
                    if len(matches) >= limit:
                        break
            else:
                continue
            break  # double-break on limit

        if output_mode == "files":
            return "\n".join(file_hits) if file_hits else ""
        elif output_mode == "count":
            return "\n".join(f"{f}:{c}" for f, c in count_map.items()) if count_map else ""
        return "\n".join(matches) if matches else ""

    def grep(
        pattern: str,
        path: str = "",
        include: str = "",
        file_type: str = "",
        case_insensitive: bool = False,
        context_lines: int = 0,
        output_mode: str = "files",
        limit: int = 0,
    ) -> str:
        """Search file contents using regex. Uses ripgrep when available, falls back to Python.

        Args:
            pattern: Regular expression to search for.
            path: File or directory to search in. Defaults to workspace root.
            include: Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}'). Comma-separated for multiple.
            file_type: Ripgrep file type filter (e.g. 'py', 'js', 'rust'). Ignored in Python fallback.
            case_insensitive: If true, ignore case when matching.
            context_lines: Number of lines to show before and after each match (content mode only).
            output_mode: 'files' returns matching file paths, 'content' returns matching lines with context, 'count' returns match counts per file.
            limit: Max results to return. 0 uses the default (200).
        """
        search_path = resolve(path) if path else workspace
        if not search_path.exists():
            return f"[error: path not found: {search_path}]"
        effective_limit = limit if limit > 0 else _GREP_DEFAULT_LIMIT

        if _has_rg:
            raw = _run_rg(
                pattern, search_path,
                case_insensitive=case_insensitive,
                include_glob=include or None,
                file_type=file_type or None,
                context_lines=context_lines,
                output_mode=output_mode,
                limit=effective_limit,
            )
        else:
            raw = _run_py_grep(
                pattern, search_path,
                case_insensitive=case_insensitive,
                include_glob=include or None,
                context_lines=context_lines,
                output_mode=output_mode,
                limit=effective_limit,
            )

        if not raw:
            return "[no matches]"

        # Apply limit (rg doesn't have a built-in result cap)
        lines = raw.splitlines()
        truncated = len(lines) > effective_limit
        lines = lines[:effective_limit]

        # Relativize absolute paths to save tokens
        ws_str = str(workspace)
        rel_lines = []
        for line in lines:
            if line.startswith(ws_str):
                line = line[len(ws_str):].lstrip(os.sep).lstrip("/")
            rel_lines.append(line)

        result = "\n".join(rel_lines)
        if output_mode == "files":
            n = len(rel_lines)
            header = f"[{n} file{'s' if n != 1 else ''} matched]"
            if truncated:
                header += f" (truncated to {effective_limit}, use limit= for more)"
            return f"{header}\n{result}"
        elif output_mode == "count":
            total = 0
            for line in rel_lines:
                parts = line.rsplit(":", 1)
                if len(parts) == 2 and parts[1].isdigit():
                    total += int(parts[1])
            return f"[{total} matches across {len(rel_lines)} files]\n{result}"
        else:
            if truncated:
                result += f"\n[truncated to {effective_limit} lines]"
            return result

    # ------------------------------------------------------------------
    # glob — file pattern search
    # ------------------------------------------------------------------

    _GLOB_DEFAULT_LIMIT = 100

    def glob_search(
        pattern: str,
        path: str = "",
        limit: int = 0,
    ) -> str:
        """Find files by name using glob patterns. Returns paths sorted by modification time (newest first).

        Args:
            pattern: Glob pattern to match (e.g. '**/*.py', 'src/**/*.ts', '*.md').
            path: Directory to search in. Defaults to workspace root.
            limit: Max files to return. 0 uses the default (100).
        """
        search_path = resolve(path) if path else workspace
        if not search_path.exists():
            return f"[error: path not found: {search_path}]"
        effective_limit = limit if limit > 0 else _GLOB_DEFAULT_LIMIT

        try:
            matches = list(search_path.glob(pattern))
        except ValueError as exc:
            return f"[error: invalid glob pattern — {exc}]"

        # Filter out VCS directories
        matches = [
            m for m in matches
            if not any(part in _VCS_DIRS for part in m.parts)
        ]

        # Sort by modification time (newest first), with name as tiebreaker
        def _sort_key(p: Path):
            try:
                return (-p.stat().st_mtime, str(p))
            except OSError:
                return (0, str(p))
        matches.sort(key=_sort_key)

        truncated = len(matches) > effective_limit
        matches = matches[:effective_limit]

        if not matches:
            return "[no files found]"

        # Relativize paths
        rel_paths = []
        for m in matches:
            try:
                rel_paths.append(str(m.relative_to(workspace)))
            except ValueError:
                rel_paths.append(str(m))

        header = f"[{len(rel_paths)} file{'s' if len(rel_paths) != 1 else ''} found]"
        if truncated:
            header += f" (truncated to {effective_limit}, use limit= for more)"
        return f"{header}\n" + "\n".join(rel_paths)

    # ------------------------------------------------------------------
    # Register all tools
    # ------------------------------------------------------------------

    agent.tool_handler.register_tool(shell,       always_on=True)
    agent.tool_handler.register_tool(view,        always_on=True)
    agent.tool_handler.register_tool(write_file,  always_on=True)
    agent.tool_handler.register_tool(str_replace, always_on=True)
    agent.tool_handler.register_tool(grep,        always_on=True)
    agent.tool_handler.register_tool(glob_search, always_on=True)

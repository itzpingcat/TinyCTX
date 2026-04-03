"""
modules/filesystem/__main__.py

Registers filesystem tools (shell, view, write_file, str_replace) into the
agent loop's tool_handler. No imports from utils or contracts — everything
arrives through the agent argument.

Shell execution is delegated to shell.py, which handles platform detection,
blacklist enforcement, and subprocess dispatch.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
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

    agent.tool_handler.register_tool(shell,       always_on=True)
    agent.tool_handler.register_tool(view,        always_on=True)
    agent.tool_handler.register_tool(write_file,  always_on=True)
    agent.tool_handler.register_tool(str_replace, always_on=True)

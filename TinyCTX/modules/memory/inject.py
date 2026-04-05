"""
modules/memory/inject.py

File injection with XML fencing and macro expansion.

Usage
-----
    from modules.memory.inject import make_provider

    agent.context.register_prompt(
        "soul",
        make_provider(soul_path, workspace, extra_macros=resolver),
        role="system",
        priority=0,
    )

Fencing
-------
Each file is wrapped in an XML-style fence using the filename as the tag name:

    <SOUL.md>
    ...file content...
    </SOUL.md>

This makes it unambiguous to the model which file it is reading when multiple
files are injected into the same system prompt.

Macros
------
Macros are {key} placeholders expanded in file content at assemble time.
Built-in macros (always available):

    {date}       — today's date, YYYY-MM-DD
    {datetime}   — current local datetime, YYYY-MM-DD HH:MM
    {workspace}  — absolute workspace path

Additional macros are supplied via the ``extra_macros`` dict passed to
``make_provider()``, or via a ``MacroResolver`` for dynamic values (e.g.
agent/user names resolved from config at call time).

Unknown {placeholders} that don't match any macro key are left as-is so
literal braces in markdown content aren't corrupted.
"""
from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MACRO_RE = re.compile(r"\{(\w+)\}")


def _read(path: Path) -> str | None:
    """Return file text stripped of leading/trailing whitespace, or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception as exc:
        logger.warning("[inject] could not read %s: %s", path, exc)
        return None


def _tag(path: Path) -> str:
    """XML tag name derived from the filename, e.g. 'SOUL.md'."""
    return path.name


def _expand(text: str, macros: dict[str, str]) -> str:
    """Replace {key} placeholders found in macros; leave unknown ones intact."""
    def replace(m: re.Match) -> str:
        key = m.group(1)
        return macros.get(key, m.group(0))  # keep original if key unknown
    return _MACRO_RE.sub(replace, text)


def _builtin_macros(workspace: Path) -> dict[str, str]:
    now = datetime.datetime.now()
    return {
        "date":      now.strftime("%Y-%m-%d"),
        "datetime":  now.strftime("%Y-%m-%d %H:%M"),
        "workspace": str(workspace),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class MacroResolver:
    """
    Collects macro sources and resolves them lazily at expansion time.

    Sources added via add_static() are fixed strings.
    Sources added via add_dynamic() are callables invoked fresh each turn.

    Dynamic sources take precedence over static ones when keys conflict.
    """

    def __init__(self) -> None:
        self._static:  dict[str, str]                    = {}
        self._dynamic: list[Callable[[], dict[str, str]]] = []

    def add_static(self, key: str, value: str) -> None:
        self._static[key] = value

    def add_dynamic(self, fn: Callable[[], dict[str, str]]) -> None:
        self._dynamic.append(fn)

    def resolve(self) -> dict[str, str]:
        merged = dict(self._static)
        for fn in self._dynamic:
            try:
                merged.update(fn())
            except Exception as exc:
                logger.warning("[inject] macro resolver fn failed: %s", exc)
        return merged


def make_provider(
    path: Path,
    workspace: Path,
    extra_macros: dict[str, str] | MacroResolver | None = None,
) -> Callable:
    """
    Return a prompt-provider callable compatible with Context.register_prompt().

    The callable accepts a context argument (ignored) and returns the fenced,
    macro-expanded file content, or None if the file is missing/empty.

    Args:
        path:         Absolute path to the markdown file to inject.
        workspace:    Workspace root — used for built-in {workspace} macro.
        extra_macros: Optional additional macros. Either a plain dict of
                      {key: value} pairs, or a MacroResolver for dynamic values.
    """
    tag = _tag(path)

    def _provider(_ctx) -> str | None:
        text = _read(path)
        if text is None:
            return None

        # Build macro table fresh each call so {date} etc. are always current
        macros = _builtin_macros(workspace)

        if isinstance(extra_macros, MacroResolver):
            macros.update(extra_macros.resolve())
        elif isinstance(extra_macros, dict):
            macros.update(extra_macros)

        expanded = _expand(text, macros)
        return f"<{tag}>\n{expanded}\n</{tag}>"

    return _provider

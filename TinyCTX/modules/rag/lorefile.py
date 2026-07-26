"""
modules/rag/lorefile.py

Native lore document format: a markdown file with a YAML frontmatter header
describing a keyword-triggered RAG entry. Replaces SillyTavern lorebook JSON
as the format for keyword-matched auto-inject entries.

Frontmatter schema (all keys optional):
    name:            str          — display label (prepended to indexed text)
    keys:            list[str]     — primary trigger keywords
    secondary_keys:  list[str]     — secondary keywords for selective logic
    constant:        bool          — always fires regardless of keyword match
    selective:       bool          — whether secondary_keys/selective_logic apply
    selective_logic: "and_any" | "not_all" | "not_any" | "and_all" (or 0-3)
    case_sensitive:  bool
    whole_words:     bool
    disabled:        bool          — skip this entry entirely

Everything after the closing '---' is the entry's content body.

A file with no (or malformed) frontmatter parses as a plain entry: no keys,
not constant, full file text as content — identical to how a bare markdown
memory file behaves today.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?", re.DOTALL)

# selectiveLogic values, ported from SillyTavern's world-info.js:33-38
_LOGIC_NAMES = {
    "and_any": 0,  # primary hit OR secondary hit
    "not_all": 1,  # primary hit AND NOT all secondary keys present
    "not_any": 2,  # primary hit AND none of secondary keys present
    "and_all": 3,  # primary hit AND all secondary keys present
}
_LOGIC_LABELS = {v: k for k, v in _LOGIC_NAMES.items()}


def normalize_selective_logic(value) -> int:
    """Accept an int 0-3 or its string alias; default to 0 (AND_ANY)."""
    if isinstance(value, str):
        return _LOGIC_NAMES.get(value.strip().lower(), 0)
    if isinstance(value, int) and value in _LOGIC_LABELS:
        return value
    return 0


@dataclass
class LoreEntry:
    name:            str = ""
    keys:            list[str] = field(default_factory=list)
    secondary_keys:  list[str] = field(default_factory=list)
    constant:        bool = False
    selective:       bool = False
    selective_logic: int = 0
    case_sensitive:  bool = False
    whole_words:     bool = False
    disabled:        bool = False
    content:         str = ""
    path:            Path | None = None


def parse_lore_doc(text: str, path: Path | None = None) -> LoreEntry:
    """Parse a native lore markdown doc into a LoreEntry."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return LoreEntry(content=text, path=path)

    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        logger.warning("[rag/lorefile] bad frontmatter in %s: %s", path, exc)
        return LoreEntry(content=text, path=path)

    if not isinstance(meta, dict):
        meta = {}

    body = text[m.end():]

    keys = meta.get("keys") or meta.get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    secondary_keys = meta.get("secondary_keys") or meta.get("keysecondary") or []
    if isinstance(secondary_keys, str):
        secondary_keys = [secondary_keys]

    return LoreEntry(
        name            = str(meta.get("name") or (path.stem if path else "")),
        keys            = [str(k) for k in keys],
        secondary_keys  = [str(k) for k in secondary_keys],
        constant        = bool(meta.get("constant", False)),
        selective       = bool(meta.get("selective", False)),
        selective_logic = normalize_selective_logic(meta.get("selective_logic", 0)),
        case_sensitive  = bool(meta.get("case_sensitive", False)),
        whole_words     = bool(meta.get("whole_words", False)),
        disabled        = bool(meta.get("disabled", False)),
        content         = body,
        path            = path,
    )


def render_lore_doc(entry: LoreEntry) -> str:
    """Serialize a LoreEntry back into native frontmatter + body markdown text."""
    meta = {
        "name":            entry.name,
        "keys":            entry.keys,
        "secondary_keys":  entry.secondary_keys,
        "constant":        entry.constant,
        "selective":       entry.selective,
        "selective_logic": _LOGIC_LABELS.get(entry.selective_logic, "and_any"),
        "case_sensitive":  entry.case_sensitive,
        "whole_words":     entry.whole_words,
        "disabled":        entry.disabled,
    }
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    body = entry.content.strip("\n")
    return f"---\n{front}\n---\n\n{body}\n"


_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slugify(name: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("_", name).strip("_").lower()
    return slug or fallback


def convert_lorebook_json(json_path: Path, dest_dir: Path) -> int:
    """
    Convert a SillyTavern lorebook/worldinfo JSON file into one native lore
    markdown doc per active entry, written under `dest_dir`.

    Does not touch `json_path` itself — the caller renames it to `.bak` once
    conversion succeeds. Returns the number of docs written (0 on failure).
    """
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("[rag/lorefile] failed to read lorebook %s: %s", json_path, exc)
        return 0

    entries_raw = raw.get("entries", {})
    if isinstance(entries_raw, dict):
        entries = list(entries_raw.values())
    elif isinstance(entries_raw, list):
        entries = entries_raw
    else:
        logger.warning("[rag/lorefile] unexpected entries format in %s", json_path)
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    written = 0

    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        uid            = str(e.get("uid", e.get("id", i)))
        comment        = e.get("comment", "") or ""
        keys           = e.get("key") or e.get("keys", [])
        secondary_keys = e.get("keysecondary") or e.get("secondary_keys", [])

        entry = LoreEntry(
            name            = comment or uid,
            keys            = [str(k) for k in keys],
            secondary_keys  = [str(k) for k in secondary_keys],
            constant        = bool(e.get("constant", False)),
            selective       = bool(e.get("selective", False)),
            selective_logic = normalize_selective_logic(e.get("selectiveLogic", 0)),
            case_sensitive  = bool(e.get("caseSensitive", False)),
            whole_words     = bool(e.get("matchWholeWords", False)),
            disabled        = bool(e.get("disable", False)),
            content         = e.get("content", "") or "",
        )

        base = _slugify(entry.name, f"entry_{uid}")
        slug = base
        n = 2
        while slug in used_names:
            slug = f"{base}_{n}"
            n += 1
        used_names.add(slug)

        out_path = dest_dir / f"{slug}.md"
        out_path.write_text(render_lore_doc(entry), encoding="utf-8")
        written += 1

    logger.info(
        "[rag/lorefile] converted %s -> %s (%d entr%s)",
        json_path, dest_dir, written, "y" if written == 1 else "ies",
    )
    return written

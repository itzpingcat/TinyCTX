"""
modules/skills/__main__.py

Agent Skills support — agentskills.io open standard.

Discovery
---------
On every assemble() the module rescans configured directories for folders
containing a SKILL.md file. Only the YAML frontmatter (name + description) is
parsed at discovery time — full content stays on disk until activated.

Scan order (first-found wins for duplicate names):
  1. Paths listed in default_config["skill_dirs"], resolved relative to workspace
  2. ~/.agents/skills/  — cross-client user convention
  3. .agents/skills/    — cross-client project convention (cwd)
  4. ~/.tinyctx/skills/ — tinyctx-specific user fallback

System prompt injection
-----------------------
A compact XML skill index is injected as a system prompt every turn:

  <available_skills>
    <skill>
      <n>pdf-processing</n>
      <description>Extract text and tables from PDF files…</description>
      <location>/abs/path/to/pdf-processing/SKILL.md</location>
    </skill>
    …
  </available_skills>

The index already tells the LLM everything it needs to decide — no list tool needed.

Tools registered
----------------
  use_skill(name) — loads the full SKILL.md body on demand.

Convention: register(agent) — no imports from gateway or bridges.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YAML frontmatter parser (stdlib only — frontmatter is always simple scalars)
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    m = _FM_RE.match(text)
    if not m:
        return {}
    result: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key:
            result[key] = val
    return result


def _skill_body(text: str) -> str:
    m = _FM_RE.match(text)
    return text[m.end():].strip() if m else text.strip()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _scan_dir(directory: Path, registry: dict[str, dict]) -> None:
    if not directory.is_dir():
        return
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
            fm   = _parse_frontmatter(text)
            name = fm.get("name", entry.name).strip() or entry.name
            if name in registry:
                continue  # first-found wins
            registry[name] = {
                "name":        name,
                "description": fm.get("description", "").strip(),
                "skill_md":    skill_md,
            }
        except Exception as exc:
            logger.warning("[skills] failed to parse %s: %s", skill_md, exc)


def _discover(scan_dirs: list[Path]) -> dict[str, dict]:
    registry: dict[str, dict] = {}
    for d in scan_dirs:
        _scan_dir(d, registry)
    return registry


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_index_prompt(registry: dict[str, dict]) -> str | None:
    if not registry:
        return None
    lines = ["<available_skills>"]
    for skill in registry.values():
        lines.append("  <skill>")
        lines.append(f"    <n>{skill['name']}</n>")
        lines.append(f"    <description>{skill['description'] or '(no description)'}</description>")
        lines.append(f"    <location>{skill['skill_md']}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    lines.append("\nUse the use_skill tool to load a skill's full instructions when relevant.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def register(agent) -> None:
    try:
        from modules.skills import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    workspace = Path(agent.config.memory.workspace_path).expanduser().resolve()

    configured: list[Path] = []
    for raw in cfg.get("skill_dirs", ["skills"]):
        p = Path(raw)
        configured.append(p if p.is_absolute() else workspace / p)

    convention_dirs = [
        Path.home() / ".agents" / "skills",
        Path.cwd() / ".agents" / "skills",
        Path.home() / ".tinyctx" / "skills",
    ]
    all_scan_dirs = configured + [d for d in convention_dirs if d not in configured]

    configured[0].mkdir(parents=True, exist_ok=True)

    _live: dict[str, dict[str, dict]] = {"registry": {}}

    def _refresh() -> dict[str, dict]:
        reg = _discover(all_scan_dirs)
        _live["registry"] = reg
        return reg

    # ------------------------------------------------------------------
    # System prompt — injects skill index every turn
    # ------------------------------------------------------------------

    agent.context.register_prompt(
        "skills_index",
        lambda _ctx: _build_index_prompt(_refresh()),
        role="system",
        priority=int(cfg.get("index_priority", 5)),
    )

    # ------------------------------------------------------------------
    # Tool: use_skill
    # ------------------------------------------------------------------

    def use_skill(name: str) -> str:
        """
        Load the full instructions for a skill into context.
        Call this when you decide a skill is relevant for the current task.

        Args:
            name: The skill name exactly as shown in <available_skills>.
        """
        reg = _live["registry"]
        if name not in reg:
            matches = [k for k in reg if k.lower() == name.lower()]
            if not matches:
                available = ", ".join(sorted(reg.keys())) or "(none)"
                return f"[error: skill '{name}' not found. Available: {available}]"
            name = matches[0]
        try:
            text = reg[name]["skill_md"].read_text(encoding="utf-8")
            body = _skill_body(text)
            return f"# Skill: {name}\n\n{body}" if body else f"[skill '{name}' has no instructions body]"
        except Exception as exc:
            return f"[error reading skill '{name}': {exc}]"

    agent.tool_handler.register_tool(use_skill)

    initial = _refresh()
    if initial:
        logger.info("[skills] discovered %d skill(s): %s", len(initial), ", ".join(sorted(initial.keys())))
    else:
        logger.info("[skills] no skills found yet — place skills in %s", configured[0])
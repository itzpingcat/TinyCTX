"""
modules/skills/__main__.py

Agent Skills support — agentskills.io open standard.

Discovery
---------
On every assemble() the module rescans configured directories for folders
containing a SKILL.md file (skills) or a DESCRIPTION.md file (categories).
Categories can be nested arbitrarily deep. Only frontmatter is parsed at
discovery time — full content stays on disk until activated.

Scan order (first-found wins for duplicate names):
  1. Paths listed in default_config["skill_dirs"], resolved relative to workspace
  2. ~/.agents/skills/  — cross-client user convention
  3. .agents/skills/    — cross-client project convention (cwd)
  4. ~/.tinyctx/skills/ — tinyctx-specific user fallback

A folder with SKILL.md is a skill. A folder with DESCRIPTION.md is a category.
If both exist, SKILL.md wins (logged as warning). Nesting is unlimited.

System prompt injection
-----------------------
A compact XML skill index is injected as a system prompt every turn.
Top-level skills render in full. Categories render as collapsed stubs.
A single <skill_category_hint> at the bottom explains how to expand them —
one hint for all collapsed categories rather than a repeated note per entry.

When ephemeral_categories=false (config), previously expanded categories are
re-expanded inline in the system prompt every turn, using the
"skills_expanded_categories" key stored in the conversation state_delta chain.

Tools registered
----------------
  use_skill(name)                    — always_on (configurable)
      Loads a skill's SKILL.md body, OR expands a category listing.
  collapse_skill_categories(paths)   — deferred
      Removes one or more category paths from the expanded set (no-op when ephemeral).

Convention: register_agent(agent) — no imports from gateway or bridges.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from TinyCTX.context import HOOK_PRE_ASSEMBLE_ASYNC, HOOK_TRANSFORM_TURN, HOOK_POST_ASSEMBLE, ROLE_TOOL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YAML frontmatter parser (stdlib only — always simple scalars)
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
# Tree nodes
# ---------------------------------------------------------------------------

class SkillEntry:
    __slots__ = ("name", "description", "skill_md", "category_path")

    def __init__(self, name: str, description: str, skill_md: Path, category_path: str) -> None:
        self.name          = name
        self.description   = description
        self.skill_md      = skill_md
        self.category_path = category_path   # "" for top-level skills


class CategoryNode:
    __slots__ = ("name", "path", "description", "skills", "subcategories")

    def __init__(self, name: str, path: str, description: str) -> None:
        self.name          = name
        self.path          = path            # slash-joined ancestor names, e.g. "agents/orchestration"
        self.description   = description
        self.skills:        list[SkillEntry]  = []
        self.subcategories: list[CategoryNode] = []


# ---------------------------------------------------------------------------
# Discovery  (recursive, arbitrary depth)
# ---------------------------------------------------------------------------

def _scan_tree(
    directory: Path,
    skills: dict[str, SkillEntry],
    categories: dict[str, CategoryNode],
    parent_path: str = "",
) -> list[SkillEntry | CategoryNode]:
    """
    Recursively scan *directory*. Returns the direct children (skills +
    category nodes) found at this level, in sorted order.

    *skills*     — flat registry: name → SkillEntry  (all depths)
    *categories* — flat registry: path → CategoryNode (all depths)
    """
    if not directory.is_dir():
        return []

    children: list[SkillEntry | CategoryNode] = []

    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue

        skill_md   = entry / "SKILL.md"
        desc_md    = entry / "DESCRIPTION.md"
        has_skill  = skill_md.exists()
        has_desc   = desc_md.exists()

        if has_skill and has_desc:
            logger.warning(
                "[skills] %s has both SKILL.md and DESCRIPTION.md — treating as skill",
                entry,
            )

        if has_skill:
            # ---- skill ----
            try:
                text = skill_md.read_text(encoding="utf-8")
                fm   = _parse_frontmatter(text)
                name = (fm.get("name", "") or entry.name).strip() or entry.name
            except Exception as exc:
                logger.warning("[skills] failed to parse %s: %s", skill_md, exc)
                continue

            if name in skills:
                logger.warning(
                    "[skills] duplicate skill name '%s' at %s — skipping (first-found wins)",
                    name, skill_md,
                )
                continue

            skill = SkillEntry(
                name=name,
                description=(fm.get("description", "") or "").strip(),
                skill_md=skill_md,
                category_path=parent_path,
            )
            skills[name] = skill
            children.append(skill)

        elif has_desc:
            # ---- category ----
            cat_path = f"{parent_path}/{entry.name}" if parent_path else entry.name

            if cat_path in categories:
                logger.warning(
                    "[skills] duplicate category path '%s' at %s — skipping",
                    cat_path, entry,
                )
                continue

            try:
                text = desc_md.read_text(encoding="utf-8")
                fm   = _parse_frontmatter(text)
                desc = (fm.get("description", "") or "").strip()
            except Exception as exc:
                logger.warning("[skills] failed to parse %s: %s", desc_md, exc)
                desc = ""

            node = CategoryNode(name=entry.name, path=cat_path, description=desc)
            categories[cat_path] = node

            # recurse
            sub_children = _scan_tree(entry, skills, categories, parent_path=cat_path)
            for child in sub_children:
                if isinstance(child, SkillEntry):
                    node.skills.append(child)
                else:
                    node.subcategories.append(child)

            children.append(node)
        # else: plain folder with neither — silently ignored

    return children


def _discover(scan_dirs: list[Path]) -> tuple[dict[str, SkillEntry], dict[str, CategoryNode], list]:
    """
    Returns:
        skills     — flat dict: name → SkillEntry
        categories — flat dict: path → CategoryNode
        top_level  — ordered list of SkillEntry | CategoryNode at root level
    """
    skills:     dict[str, SkillEntry]   = {}
    categories: dict[str, CategoryNode] = {}
    top_level:  list                    = []

    for d in scan_dirs:
        for child in _scan_tree(d, skills, categories):
            # top-level dedup: first-found wins for same name/path
            if isinstance(child, SkillEntry):
                if child in top_level:
                    continue
            else:
                if any(isinstance(c, CategoryNode) and c.path == child.path for c in top_level):
                    continue
            top_level.append(child)

    return skills, categories, top_level


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _render_category_expanded(node: CategoryNode, indent: int = 2) -> list[str]:
    """Render a CategoryNode fully expanded (used inside system prompt when persistent)."""
    pad = " " * indent
    lines = [
        f"{pad}<skill_category name={node.name!r} path={node.path!r}>",
        f"{pad}  <description>{node.description or '(no description)'}</description>",
    ]
    for skill in node.skills:
        lines += [
            f"{pad}  <skill>",
            f"{pad}    <n>{skill.name}</n>",
            f"{pad}    <description>{skill.description or '(no description)'}</description>",
            f"{pad}    <location>{skill.skill_md}</location>",
            f"{pad}  </skill>",
        ]
    for sub in node.subcategories:
        lines += _render_category_expanded(sub, indent + 2)
    lines.append(f"{pad}</skill_category>")
    return lines


def _render_category_collapsed(node: CategoryNode, indent: int = 2) -> list[str]:
    pad = " " * indent
    return [
        f"{pad}<skill_category name={node.name!r} path={node.path!r}>",
        f"{pad}  <description>{node.description or '(no description)'}</description>",
        f"{pad}</skill_category>",
    ]


def _build_index_prompt(
    top_level: list,
    expanded: set[str],
    categories: dict[str, CategoryNode],
) -> str | None:
    if not top_level:
        return None

    lines = ["<available_skills>"]
    has_collapsed = False

    for child in top_level:
        if isinstance(child, SkillEntry):
            lines += [
                "  <skill>",
                f"    <n>{child.name}</n>",
                f"    <description>{child.description or '(no description)'}</description>",
                f"    <location>{child.skill_md}</location>",
                "  </skill>",
            ]
        else:
            if child.path in expanded:
                lines += _render_category_expanded(child, indent=2)
            else:
                lines += _render_category_collapsed(child, indent=2)
                has_collapsed = True

    if has_collapsed:
        lines.append("  <skill_category_hint>Call use_skill(path) on any skill_category to expand it and reveal its contents.</skill_category_hint>")

    lines.append("</available_skills>")
    lines.append("\nUse the use_skill tool to load a skill's full instructions or expand a category.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Category expansion renderer  (returned as tool output)
# ---------------------------------------------------------------------------

def _expand_category_text(node: CategoryNode, depth: int = 0) -> str:
    """
    Build a human/LLM-readable expansion of a category node.
    Sub-categories are shown as stubs — call use_skill(path) to go deeper.
    """
    lines: list[str] = []
    indent = "  " * depth

    if depth == 0:
        lines.append(f"# Category: {node.path}")
        if node.description:
            lines.append(node.description)
        lines.append("")

    for skill in node.skills:
        lines.append(f"{indent}## {skill.name}")
        if skill.description:
            lines.append(f"{indent}{skill.description}")
        lines.append(f"{indent}Location: {skill.skill_md}")
        lines.append("")

    for sub in node.subcategories:
        lines.append(f"{indent}## {sub.name}  [category]")
        if sub.description:
            lines.append(f"{indent}{sub.description}")
        lines.append(f"{indent}→ Call use_skill(\"{sub.path}\") to expand.")
        lines.append("")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# State helpers  (skills_expanded_categories key in state_delta chain)
# ---------------------------------------------------------------------------

_STATE_KEY = "skills_expanded_categories"

# ---------------------------------------------------------------------------
# "Skill fell out of context" tracking
# ---------------------------------------------------------------------------
# A use_skill() tool result gets tagged f"{_TAG_PREFIX}{name}" — derived from
# the ORIGINATING TOOL CALL's name/arguments (structured data, never parsed
# from the result's content). If that tagged entry later gets dropped or
# gutted (by ctx_tools' trim/tokenade, by dedup, or by the final token-budget
# loop), Context.assemble() reports it via AssembleMeta.invalidated_tags —
# see context.py's "Tag survival" comment. A post_assemble hook here reads
# ctx.state["invalidated_tags"] (set before post_assemble runs) and persists
# the dropped skill names; a footer prompt surfaces the reminder once on the
# NEXT turn, then clears it. This never needs to inspect string content.

_TAG_PREFIX = "skill:"
_STATE_KEY_DROPPED  = "skills_dropped"    # names to surface once, then clear
_STATE_KEY_RESIDENT = "skills_resident"   # names currently believed present

# NOTE on why "resident" tracking exists: the DB node backing a use_skill()
# result is never deleted just because ctx_tools trims it out of one turn's
# assembled view. That means the SAME entry gets independently re-tagged and
# re-aged-out on every subsequent turn forever — without tracking which
# skills were previously resident, invalidated_tags would report "foo" as
# dropped on every single turn after the first, not just once. Resident
# tracking turns "is X currently invalidated" (true forever once it ages out)
# into "did X just transition from present to gone" (true exactly once).


def _load_names(agent, key: str) -> list[str]:
    try:
        raw = agent.db.get_state(agent.context.tail_node_id, key, "[]")
        return json.loads(raw)
    except Exception:
        return []


def _save_names(agent, key: str, names: list[str]) -> None:
    try:
        agent.db.set_state(agent.context.tail_node_id, key, json.dumps(sorted(set(names))))
    except Exception as exc:
        logger.warning("[skills] failed to persist %s: %s", key, exc)


def _load_dropped(agent) -> list[str]:
    return _load_names(agent, _STATE_KEY_DROPPED)


def _save_dropped(agent, names: list[str]) -> None:
    _save_names(agent, _STATE_KEY_DROPPED, names)


def _load_expanded(agent) -> set[str]:
    try:
        raw = agent.db.get_state(agent.context.tail_node_id, _STATE_KEY, "[]")
        return set(json.loads(raw))
    except Exception:
        return set()


def _save_expanded(agent, expanded: set[str]) -> None:
    try:
        # set_state merge-writes just this key onto the tail node, so it
        # can't clobber another module's key already written on this node.
        agent.db.set_state(
            agent.context.tail_node_id,
            _STATE_KEY,
            json.dumps(sorted(expanded)),
        )
    except Exception as exc:
        logger.warning("[skills] failed to persist expanded categories: %s", exc)


# ---------------------------------------------------------------------------
# register_agent
# ---------------------------------------------------------------------------

def register_agent(cycle) -> None:
    agent = cycle
    try:
        from TinyCTX.modules.skills import EXTENSION_META
        cfg: dict = dict(EXTENSION_META.get("default_config", {}))
    except ImportError:
        cfg = {}

    # Merge config.yaml overrides (under top-level 'skills:' key)
    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        for k, v in agent.config.extra.get("skills", {}).items():
            cfg[k] = v

    ephemeral: bool = bool(cfg.get("ephemeral_categories", True))

    workspace = Path(agent.config.workspace.path).expanduser().resolve()

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

    _live: dict[str, Any] = {
        "skills":     {},   # name → SkillEntry
        "categories": {},   # path → CategoryNode
        "top_level":  [],   # ordered root-level items
        "last_scan":  0.0,  # monotonic time of last completed _discover()
    }
    _refresh_lock = asyncio.Lock()

    # Re-scan at most this often (seconds). The filesystem walk runs in a
    # worker thread (see _refresh_async below) so it never blocks the loop,
    # but we still cap frequency to avoid hammering disk every turn.
    rescan_interval = float(cfg.get("rescan_interval_seconds", 30))

    def _refresh_sync() -> tuple[dict, dict, list]:
        """Blocking discovery scan. Only ever call this from a worker thread
        (via asyncio.to_thread) — never directly on the event loop."""
        skills, categories, top_level = _discover(all_scan_dirs)
        _live["skills"]     = skills
        _live["categories"] = categories
        _live["top_level"]  = top_level
        _live["last_scan"]  = time.monotonic()
        return skills, categories, top_level

    async def _refresh_async(force: bool = False) -> None:
        """Refresh the skills cache off the event loop. Safe to call from a
        HOOK_PRE_ASSEMBLE_ASYNC hook every turn — it no-ops unless the cache
        is stale (or force=True), and the actual disk walk runs in a thread."""
        if not force and (time.monotonic() - _live["last_scan"]) < rescan_interval:
            return
        async with _refresh_lock:
            # Re-check inside the lock in case another coroutine just refreshed.
            if not force and (time.monotonic() - _live["last_scan"]) < rescan_interval:
                return
            try:
                await asyncio.to_thread(_refresh_sync)
            except Exception:
                logger.exception("[skills] background rescan failed")

    async def _pre_assemble_refresh(_ctx) -> None:
        await _refresh_async()

    agent.context.register_hook(
        HOOK_PRE_ASSEMBLE_ASYNC,
        _pre_assemble_refresh,
    )

    # ------------------------------------------------------------------
    # System prompt — injects skill index every turn
    # ------------------------------------------------------------------
    # NOTE: this is called synchronously inside Context.assemble() (the
    # provider contract there is sync-only by design — see context.py).
    # It must never touch disk. It only reads the in-memory cache, which
    # is kept warm by _pre_assemble_refresh above (awaited by the agent
    # via run_async_hooks(HOOK_PRE_ASSEMBLE_ASYNC) BEFORE assemble() runs).

    def _build_prompt(_ctx) -> str | None:
        top_level  = _live["top_level"]
        categories = _live["categories"]
        expanded = set() if ephemeral else _load_expanded(agent)
        return _build_index_prompt(top_level, expanded, categories)

    agent.context.register_prompt(
        "skills_index",
        _build_prompt,
        role="system",
        priority=int(cfg.get("index_priority", 5)),
    )

    # ------------------------------------------------------------------
    # Tool: use_skill
    # ------------------------------------------------------------------

    def use_skill(name: str) -> str:
        """
        Load the full instructions for a skill, or expand a skill category to
        see what's inside it.

        For skills: loads and returns the full SKILL.md body.
        For categories: returns a listing of skills and sub-categories inside.
        Use the path shown in <skill_category path="..."> to address a category,
        e.g. use_skill("agents") or use_skill("agents/orchestration").

        Args:
            name: Skill name or category path as shown in <available_skills>.
        """
        skills     = _live["skills"]
        categories = _live["categories"]

        # --- category match (exact path) ---
        if name in categories:
            node   = categories[name]
            result = _expand_category_text(node)

            if not ephemeral:
                expanded = _load_expanded(agent)
                expanded.add(name)
                _save_expanded(agent, expanded)

            return result

        # --- skill match ---
        if name in skills:
            skill = skills[name]
            try:
                text = skill.skill_md.read_text(encoding="utf-8")
                body = _skill_body(text)
                return f"# Skill: {name}\n\n{body}" if body else f"Skill '{name}' has no instructions body"
            except Exception as exc:
                return f"Error reading skill '{name}': {exc}"

        # --- case-insensitive fallback ---
        name_lower  = name.lower()
        cat_match   = next((p for p in categories if p.lower() == name_lower), None)
        if cat_match:
            return use_skill(cat_match)
        skill_match = next((n for n in skills if n.lower() == name_lower), None)
        if skill_match:
            return use_skill(skill_match)

        # --- not found ---
        available_skills = ", ".join(sorted(skills.keys())) or "(none)"
        available_cats   = ", ".join(sorted(categories.keys())) or "(none)"
        return (
            f"Error: '{name}' not found.\n"
            f"  Skills: {available_skills}\n"
            f"  Categories: {available_cats}"
        )

    _sk_vis = str(cfg.get("tools", {}).get("use_skill", "always_on")).lower().strip()
    if _sk_vis != "disabled":
        agent.tool_handler.register_tool(
            use_skill,
            always_on=(_sk_vis != "deferred"),
            min_permission=25,
        )

    # ------------------------------------------------------------------
    # Tool: collapse_skill_categories  (deferred)
    # ------------------------------------------------------------------

    def collapse_skill_categories(paths: list[str]) -> str:
        """
        Remove one or more category paths from the expanded set so they return
        to collapsed (stub) view in the system prompt.

        Has no effect when ephemeral_categories=true (the default), since
        categories are never persistently expanded in that mode.

        Args:
            paths: List of category paths to collapse, e.g. ["agents", "agents/orchestration"].
                   Pass ["*"] to collapse all expanded categories at once.
        """
        if ephemeral:
            return "Ephemeral mode is on — nothing to collapse"

        expanded = _load_expanded(agent)
        if not expanded:
            return "No categories are currently expanded"

        if paths == ["*"]:
            collapsed = sorted(expanded)
            unknown   = []
            _save_expanded(agent, set())
        else:
            collapsed = sorted(p for p in paths if p in expanded)
            unknown   = sorted(p for p in paths if p not in expanded)
            _save_expanded(agent, expanded - set(collapsed))

        parts = []
        if collapsed:
            parts.append(f"Collapsed: {', '.join(collapsed)}")
        if unknown:
            parts.append(f"Not expanded (ignored): {', '.join(unknown)}")
        return "\n".join(parts) if parts else "Nothing changed"

    agent.tool_handler.register_tool(
        collapse_skill_categories,
        always_on=False,   # deferred
        min_permission=25,
    )

    # ------------------------------------------------------------------
    # Tag use_skill() results — structural, from the tool call's own
    # name/arguments, never from the result content.
    # ------------------------------------------------------------------
    # Runs at priority -100 so it's the very first transform_turn hook —
    # well before ctx_tools' dedup (0), tokenade (1), token_sanitize (2),
    # cot_strip (5), trim (10) — so those hooks see (and can correctly
    # clear) the tag if they gut the entry's content.

    def _tag_use_skill_results(entry, age, ctx):
        if entry.role != ROLE_TOOL or not entry.tool_call_id:
            return None
        for e in ctx.dialogue:
            for tc in e.tool_calls:
                if tc["id"] != entry.tool_call_id or tc["name"] != "use_skill":
                    continue
                args = tc["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                skill_name = args.get("name")
                if not skill_name:
                    return None
                from dataclasses import replace as _replace
                return _replace(entry, tags=entry.tags | {f"{_TAG_PREFIX}{skill_name}"})
        return None

    agent.context.register_hook(HOOK_TRANSFORM_TURN, _tag_use_skill_results, priority=-100)

    # ------------------------------------------------------------------
    # Capture invalidated skill tags -> persisted "skills_dropped" state
    # ------------------------------------------------------------------

    def _capture_dropped_skills(messages, ctx):
        invalidated = ctx.state.get("invalidated_tags") or frozenset()
        surviving   = ctx.state.get("surviving_tags") or frozenset()

        invalidated_now = {t[len(_TAG_PREFIX):] for t in invalidated if t.startswith(_TAG_PREFIX)}
        surviving_now   = {t[len(_TAG_PREFIX):] for t in surviving   if t.startswith(_TAG_PREFIX)}

        resident = set(_load_names(agent, _STATE_KEY_RESIDENT))

        # Only a skill that WAS resident and is now invalidated counts as a
        # fresh drop worth reporting — this is the one-time transition, not
        # "is it currently gone" (which stays true forever once it ages out).
        newly_dropped = resident & invalidated_now
        new_resident  = (resident - newly_dropped) | surviving_now

        _save_names(agent, _STATE_KEY_RESIDENT, list(new_resident))

        if newly_dropped:
            existing = set(_load_dropped(agent))
            _save_dropped(agent, list(existing | newly_dropped))
        return None

    agent.context.register_hook(HOOK_POST_ASSEMBLE, _capture_dropped_skills)

    # ------------------------------------------------------------------
    # Footer — surfaces the reminder once, then clears it
    # ------------------------------------------------------------------

    def _dropped_skills_footer(ctx) -> str | None:
        session = ctx.state.get("session", {}) if ctx is not None else {}
        names = session.get(_STATE_KEY_DROPPED)
        try:
            names = json.loads(names) if isinstance(names, str) else (names or [])
        except (json.JSONDecodeError, ValueError):
            names = []
        if not names:
            return None

        # Surface once: clear immediately so it doesn't repeat next turn.
        _save_dropped(agent, [])

        listed = ", ".join(f'"{n}"' for n in names)
        return (
            f"<skill_reminder>The following skill(s) fell out of context and may no "
            f"longer be loaded: {listed}. Call use_skill(name) again if you still "
            f"need their instructions.</skill_reminder>"
        )

    agent.context.register_prompt(
        "skills_dropped_footer",
        _dropped_skills_footer,
        role="user",
        priority=int(cfg.get("dropped_footer_priority", 50)),
    )

    # ------------------------------------------------------------------
    # Initial scan + logging
    # ------------------------------------------------------------------

    skills, categories, _ = _refresh_sync()
    n_skills = len(skills)
    n_cats   = len(categories)

    if n_skills or n_cats:
        logger.info(
            "[skills] discovered %d skill(s), %d categor%s: skills=[%s] categories=[%s]",
            n_skills,
            n_cats,
            "y" if n_cats == 1 else "ies",
            ", ".join(sorted(skills.keys())),
            ", ".join(sorted(categories.keys())),
        )
    else:
        logger.info("[skills] no skills found yet — place skills in %s", configured[0])

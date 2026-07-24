"""
modules/memory/reviewer.py

Reviewer librarian orchestrator: loads flaggers, scans the graph, maintains a
persisted, de-duplicated issue queue, and processes it with adaptive throttling.

Flagger contract (each module in flaggers/):
    FLAGGER_TYPE: str
    scan(graph_db, cfg) -> list[dict]   # issue dicts (see _norm_issue)
    build_prompt(issue) -> str          # reviewer instruction for one issue

Issue dict keys: flagger_type, entity_uuids (list), scope (str), detail (str).
Dedup key = (flagger_type, tuple(sorted(entity_uuids))).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import pkgutil
from pathlib import Path

from TinyCTX.modules.memory import tools as _tools
from TinyCTX.modules.memory.librarian_common import agent_loop, make_tool_handler

logger = logging.getLogger(__name__)
_PROMPTS = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# Pure queue logic (unit-tested)
# ---------------------------------------------------------------------------

def issue_key(issue: dict) -> tuple:
    return (issue.get("flagger_type", "?"), tuple(sorted(issue.get("entity_uuids", []))))


def _norm_issue(issue: dict, default_type: str) -> dict:
    return {
        "flagger_type": issue.get("flagger_type", default_type),
        "entity_uuids": list(issue.get("entity_uuids", [])),
        "scope": issue.get("scope", "global"),
        "detail": issue.get("detail", ""),
    }


class ReviewerQueue:
    """Persisted issue queue with dedup. Survives restart (JSON at data dir)."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._issues: list[dict] = []
        self._keys: set[tuple] = set()
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._issues = data.get("issues", [])
            self._keys = {issue_key(i) for i in self._issues}
        except (OSError, ValueError):
            self._issues, self._keys = [], set()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"issues": self._issues}, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError as exc:
            logger.warning("[memory/reviewer] queue save failed: %s", exc)

    def __len__(self) -> int:
        return len(self._issues)

    def counts_by_type(self) -> dict:
        out: dict[str, int] = {}
        for i in self._issues:
            t = i.get("flagger_type", "?")
            out[t] = out.get(t, 0) + 1
        return out

    async def append_deduped(self, issues: list[dict]) -> int:
        added = 0
        async with self._lock:
            for issue in issues:
                k = issue_key(issue)
                if k not in self._keys:
                    self._keys.add(k)
                    self._issues.append(issue)
                    added += 1
            self._save()
        return added

    async def push_front(self, issue: dict) -> bool:
        async with self._lock:
            k = issue_key(issue)
            if k in self._keys:
                return False
            self._keys.add(k)
            self._issues.insert(0, issue)
            self._save()
            return True

    async def pop(self) -> dict | None:
        async with self._lock:
            if not self._issues:
                return None
            issue = self._issues.pop(0)
            self._keys.discard(issue_key(issue))
            self._save()
            return issue


# ---------------------------------------------------------------------------
# Flagger loading
# ---------------------------------------------------------------------------

def load_flaggers() -> dict:
    """Dynamically import every module in flaggers/. Returns {FLAGGER_TYPE: module}."""
    from TinyCTX.modules.memory import flaggers as flaggers_pkg
    out: dict = {}
    for info in pkgutil.iter_modules(flaggers_pkg.__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{flaggers_pkg.__name__}.{info.name}")
            ftype = getattr(mod, "FLAGGER_TYPE", info.name)
            if hasattr(mod, "scan") and hasattr(mod, "build_prompt"):
                out[ftype] = mod
        except Exception as exc:
            logger.warning("[memory/reviewer] flagger %s failed to load: %s", info.name, exc)
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_reviewer_cycle(cfg, graph_db, conn, write_lock, llm, queue: ReviewerQueue,
                             agent_logger) -> None:
    """One reviewer pass: scan flaggers, enqueue deduped, then drain with throttle."""
    flaggers = load_flaggers()

    # Scan + enqueue (append happens fully before processing — the interlock).
    new_issues: list[dict] = []
    for ftype, mod in flaggers.items():
        try:
            for issue in mod.scan(graph_db, cfg):
                new_issues.append(_norm_issue(issue, ftype))
        except Exception as exc:
            logger.warning("[memory/reviewer] flagger %s scan error: %s", ftype, exc)
    added = await queue.append_deduped(new_issues)
    if added:
        logger.info("[memory/reviewer] enqueued %d new issue(s)", added)

    base = float(cfg.get("reviewer_base_delay", 30))
    min_delay = float(cfg.get("reviewer_min_delay", 2))
    target = int(cfg.get("reviewer_target_len", 10))
    vocab = await _tools.relation_vocab()

    while True:
        issue = await queue.pop()
        if issue is None:
            break
        mod = flaggers.get(issue["flagger_type"])
        if mod is None:
            continue
        try:
            instruction = mod.build_prompt(issue)
            system = _read("reviewer_system.txt").format(relation_vocab=vocab)
            scope_set = {issue.get("scope", "global"), "global"}
            with _tools.scope_context(scope_set):
                await agent_loop(llm, system, instruction, make_tool_handler(), agent_logger)
        except Exception as exc:
            logger.warning("[memory/reviewer] processing issue failed: %s", exc)
        await asyncio.sleep(_tools.throttle_delay(len(queue), base=base, min_delay=min_delay, target=target))


def _read(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")

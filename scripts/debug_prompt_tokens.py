"""
scripts/debug_prompt_tokens.py

Debugging utility: estimates system prompt token size using tiktoken.

Counts tokens (o200k_base, matching Context._get_encoder in context.py) for
the system-prompt files injected by modules/system_prompt (SOUL.md, AGENTS.md,
TOOLS.md) and modules/equipment_manifest (EM.md), printing a per-file and
total breakdown. Also reports the two memory-retrieval token budgets:
modules/rag's result_budget_tokens (max size of the <rag_context> block per
databank per turn) and modules/memory's memory_block_tokens (max size of the
<memory> knowledge-graph-recall block per turn). Both are read from
config.yaml's `rag:`/`memory:` blocks, falling back to each module's own
default when unset.

The instance directory (and its workspace/) is auto-resolved the same way
commands/_instance.py resolves it for start/stop/status/launch. EM.md
defaults to living next to modules/equipment_manifest instead of the
workspace, per equipment_manifest's own _resolve_em_path.

Usage:
    python scripts/debug_prompt_tokens.py [--dir INSTANCE_DIR] [--file NAME ...]

--dir overrides instance-dir resolution (same flag semantics as the CLI
commands). --file overrides the default file list entirely.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import tiktoken
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from TinyCTX.commands._instance import resolve_instance_dir

DEFAULT_FILES = ["SOUL.md", "AGENTS.md", "TOOLS.md", "EM.md"]

# Repo-relative path to modules/equipment_manifest, mirroring _resolve_em_path's
# default (EM.md next to the module when no config override is set).
EM_MODULE_DIR = Path(__file__).resolve().parents[1] / "TinyCTX" / "modules" / "equipment_manifest"

# modules/rag/__init__.py EXTENSION_META["default_config"]["result_budget_tokens"]
RAG_DEFAULT_BUDGET_TOKENS = 2048

# modules/memory/__init__.py EXTENSION_META["default_config"]["memory_block_tokens"]
# — token budget for the <memory> block (knowledge-graph recall) in __main__.py
MEMORY_DEFAULT_BLOCK_TOKENS = 4096


def _load_extra_key(instance_dir: Path, key: str) -> dict:
    """Read a top-level key's dict from <instance_dir>/config.yaml — these
    pass straight through into Config.extra (see config/__main__.py's
    _KNOWN_KEYS/extra split). Returns {} if config.yaml or the key is absent."""
    config_path = instance_dir / "config.yaml"
    if not config_path.is_file():
        return {}
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return raw.get(key, {}) or {}


def get_rag_budget_tokens(instance_dir: Path) -> int:
    """rag.result_budget_tokens — max tokens the <rag_context> block may
    occupy per databank per turn."""
    cfg = _load_extra_key(instance_dir, "rag")
    return int(cfg.get("result_budget_tokens", RAG_DEFAULT_BUDGET_TOKENS))


def get_memory_block_tokens(instance_dir: Path) -> int:
    """memory.memory_block_tokens — max tokens the <memory> block (knowledge-
    graph recall, modules/memory/__main__.py) may occupy per turn."""
    cfg = _load_extra_key(instance_dir, "memory")
    return int(cfg.get("memory_block_tokens", MEMORY_DEFAULT_BLOCK_TOKENS))


def get_encoder() -> "tiktoken.Encoding | None":
    """Same fallback as Context._get_encoder in context.py: None if the
    encoding can't be loaded (e.g. no network access to fetch it)."""
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def count_tokens(text: str, enc: "tiktoken.Encoding | None") -> int:
    if enc is None:
        return len(text) // 4  # matches context.py's no-tiktoken fallback
    return len(enc.encode(text, disallowed_special=()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", dest="instance_dir", default=None, help="Explicit instance directory (overrides auto-resolution)")
    parser.add_argument("--file", action="append", dest="files", help="File name to check (repeatable); defaults to SOUL.md, AGENTS.md, TOOLS.md, EM.md")
    args = parser.parse_args()

    instance_dir = resolve_instance_dir(args.instance_dir)
    workspace = instance_dir / "workspace"
    names = args.files or DEFAULT_FILES

    # Resolve each file name to a path: workspace files live in workspace/;
    # EM.md defaults to the equipment_manifest module dir unless the caller
    # already has it in workspace (matching _resolve_em_path's precedence).
    paths: list[tuple[str, Path]] = []
    for name in names:
        path = workspace / name
        if name == "EM.md" and not path.is_file():
            path = EM_MODULE_DIR / name
        paths.append((name, path))

    enc = get_encoder()
    if enc is None:
        print("Warning: tiktoken encoding unavailable (no network?); falling back to chars/4 estimate.\n")

    total = 0
    print(f"Instance: {instance_dir}")
    print(f"Workspace: {workspace}\n")
    print(f"{'file':<20}{'tokens':>10}{'chars':>10}")
    for name, path in paths:
        if not path.is_file():
            print(f"{name:<20}{'missing':>10}")
            continue
        text = path.read_text(encoding="utf-8")
        tokens = count_tokens(text, enc)
        total += tokens
        print(f"{name:<20}{tokens:>10}{len(text):>10}")

    print(f"\n{'TOTAL':<20}{total:>10}")

    rag_budget = get_rag_budget_tokens(instance_dir)
    memory_budget = get_memory_block_tokens(instance_dir)
    print(f"\nMax <rag_context> retrieve tokens (rag.result_budget_tokens, per databank/turn): {rag_budget}")
    print(f"Max <memory> retrieve tokens (memory.memory_block_tokens, per turn): {memory_budget}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

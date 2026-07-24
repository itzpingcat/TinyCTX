#!/usr/bin/env python3
"""
calibrate_embed_threshold.py — Sanity-check an embedding model + template pair
and suggest a similarity_threshold for memory/deduper.py.

Embeds three tiers of sentence pairs shaped like memory's entity embed
strings ("Name (Type)\\nDescription"):

  duplicate  — same entity, reworded (should score HIGH)
  distinct   — different entity, same type/topic (the hard case — should
               score LOWER than duplicate, this is what dedup must not merge)
  unrelated  — different entity, different domain (should score LOWEST)

If duplicate scores don't clear distinct scores with daylight between them,
the embedding model/template can't reliably separate real duplicates from
near-neighbors at any threshold — that's what a "collapsed" embedding space
looks like (see the 18637-pairs-for-nothing dedup run this is calibrating
against).

Runs as a single batch request by default (matches rag/indexer.py's
embed(chunks)). Pass --sequential to embed one-at-a-time instead, e.g. to
check whether a problem is specific to batching.

Usage:
    python scripts/calibrate_embed_threshold.py
    python scripts/calibrate_embed_threshold.py --model embed
    python scripts/calibrate_embed_threshold.py --sequential
    python scripts/calibrate_embed_threshold.py --config path/to/config.yaml
    python scripts/calibrate_embed_threshold.py --dir path/to/instance

Config resolution (when --config isn't given or doesn't exist): resolved via
utils/instance.py, same as the CLI (--dir / CWD .tinyctx / ~/.tinyctx).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from TinyCTX import config as _config
from TinyCTX.ai import Embedder
from TinyCTX.modules.memory.graph import cosine_similarity
from TinyCTX.utils.instance import resolve_instance_dir, config_path_for


# Entity-shaped pairs, mirroring modules/memory/graph.py's embed_content_for()
# ("{name} ({type})\n{description}"). Each tuple is (label, text_a, text_b).
TEST_PAIRS: list[tuple[str, str, str]] = [
    # duplicate — same entity, reworded
    ("duplicate",
     "Alice (Person)\nSoftware engineer at Google, enjoys hiking.",
     "Alice (Person)\nA software engineer who works at Google and likes to hike."),
    ("duplicate",
     "TinyCTX (Project)\nAn LLM agent framework with modular extensions.",
     "TinyCTX (Project)\nModular framework for building LLM agents."),
    ("duplicate",
     "Project Atlas (Project)\nInternal tool for tracking quarterly OKRs.",
     "Project Atlas (Project)\nAn internal tool used to track OKRs each quarter."),

    # distinct — different entity, same type/topic (the hard case)
    ("distinct",
     "Alice (Person)\nSoftware engineer at Google, enjoys hiking.",
     "Charlie (Person)\nSoftware engineer at Google, enjoys climbing."),
    ("distinct",
     "TinyCTX (Project)\nAn LLM agent framework with modular extensions.",
     "LangChain (Project)\nA framework for building LLM applications with chains."),
    ("distinct",
     "Project Atlas (Project)\nInternal tool for tracking quarterly OKRs.",
     "Project Beacon (Project)\nInternal tool for tracking incident response."),

    # unrelated — different entity, different domain
    ("unrelated",
     "Alice (Person)\nSoftware engineer at Google, enjoys hiking.",
     "Paris (Location)\nCapital city of France, known for the Eiffel Tower."),
    ("unrelated",
     "TinyCTX (Project)\nAn LLM agent framework with modular extensions.",
     "Quantum Computing (Concept)\nComputation using qubits and superposition."),
    ("unrelated",
     "Project Atlas (Project)\nInternal tool for tracking quarterly OKRs.",
     "Bob (Person)\nEnjoys baking sourdough bread on weekends."),
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", help="Embedding model name from config.yaml's models: section (skips the prompt)")
    p.add_argument("--config", help="Explicit path to config.yaml")
    p.add_argument("--dir", help="Explicit instance directory (containing config.yaml)")
    p.add_argument("--sequential", action="store_true",
                    help="Embed one text per request instead of a single batch request. "
                         "Default is batch, since that's what rag/indexer.py does in production "
                         "and is the mode most likely to expose server-side batching bugs; use "
                         "--sequential to isolate whether a problem is batching-specific.")
    return p.parse_args()


def _load_config(args: argparse.Namespace) -> "_config.Config":
    if args.config and Path(args.config).exists():
        path = Path(args.config)
    else:
        instance_dir = resolve_instance_dir(args.dir)
        path = config_path_for(instance_dir)
    if not path.exists():
        print(f"Config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return _config.load(str(path))


def _pick_model(cfg: "_config.Config", requested: str | None) -> str:
    embedding_models = [n for n, m in cfg.models.items() if m.is_embedding]
    if not embedding_models:
        print("No models with kind='embedding' defined in config.yaml.", file=sys.stderr)
        sys.exit(1)

    if requested:
        if requested not in embedding_models:
            print(f"'{requested}' is not an embedding model. Available: {', '.join(embedding_models)}", file=sys.stderr)
            sys.exit(1)
        return requested

    print("Embedding models available in config.yaml:")
    for n in embedding_models:
        print(f"  - {n}")
    name = input(f"Which embedding model? [{embedding_models[0]}]: ").strip() or embedding_models[0]
    if name not in embedding_models:
        print(f"'{name}' is not an embedding model. Available: {', '.join(embedding_models)}", file=sys.stderr)
        sys.exit(1)
    return name


async def _run(model_name: str, cfg: "_config.Config", sequential: bool) -> None:
    model_cfg = cfg.get_embedding_model(model_name)
    embedder = Embedder.from_config(model_cfg)

    print(f"\nModel: {model_cfg.model} @ {model_cfg.base_url}")
    print(f"document_template: {model_cfg.document_template!r}")
    print(f"query_template:     {model_cfg.query_template!r}")
    print(f"mode: {'sequential (one request per text)' if sequential else 'single batch request'}\n")

    flat_texts = [t for _, a, b in TEST_PAIRS for t in (a, b)]
    if sequential:
        # No batching — isolates whether a bug lives in the server/_call()'s
        # handling of multi-text requests vs. the model/template itself.
        vectors = [await embedder.embed_one(t, kind="document") for t in flat_texts]
    else:
        # Single batch request — mirrors rag/indexer.py's embedder.embed(chunks).
        vectors = await embedder.embed(flat_texts, kind="document")

    scores: dict[str, list[float]] = {"duplicate": [], "distinct": [], "unrelated": []}
    print(f"{'label':<10} {'similarity':>10}   pair")
    print("-" * 72)
    for i, (label, a, b) in enumerate(TEST_PAIRS):
        va, vb = vectors[2 * i], vectors[2 * i + 1]
        sim = cosine_similarity(va, vb)
        scores[label].append(sim)
        a_short = a.split("\n", 1)[0]
        b_short = b.split("\n", 1)[0]
        print(f"{label:<10} {sim:>10.4f}   {a_short}  <->  {b_short}")

    print()
    for label in ("duplicate", "distinct", "unrelated"):
        vals = scores[label]
        print(f"{label:<10} min={min(vals):.4f}  max={max(vals):.4f}  avg={sum(vals)/len(vals):.4f}")

    dup_min = min(scores["duplicate"])
    distinct_max = max(scores["distinct"])
    unrelated_max = max(scores["unrelated"])

    print()
    if dup_min > distinct_max:
        threshold = (dup_min + distinct_max) / 2
        print(f"Clean separation: lowest duplicate score ({dup_min:.4f}) > highest distinct score ({distinct_max:.4f}).")
        print(f"Suggested similarity_threshold: {threshold:.4f}")
        if distinct_max < unrelated_max:
            print("Note: a 'distinct' pair scored lower than an 'unrelated' pair — check the raw table above, "
                  "may be worth adding more test pairs for your actual data before trusting this threshold blindly.")
    else:
        print("NO CLEAN SEPARATION — lowest duplicate score "
              f"({dup_min:.4f}) is <= highest distinct score ({distinct_max:.4f}).")
        print("This is what embedding collapse looks like: no single threshold distinguishes real "
              "duplicates from different-but-similar entities. Before trusting cosine dedup on this "
              "model, check document_template isn't dominating short entity content, and consider a "
              "different embedding model.")


def main() -> None:
    args = _parse_args()
    cfg = _load_config(args)
    model_name = _pick_model(cfg, args.model)
    asyncio.run(_run(model_name, cfg, args.sequential))


if __name__ == "__main__":
    main()

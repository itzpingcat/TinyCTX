"""
onboard/providers_setup.py — Step 1: LLM provider & model selection.

Handles both beginner (quickstart) and pro (standard/advanced) flows.
Returns provider config dicts ready to be merged into config.yaml.
"""

from __future__ import annotations

import sys
from typing import Any

from .helpers import GoBack, Mode, c, section, success, warn
from .providers import configure_provider, configure_provider_quickstart


def run(providers: dict, beginner_providers: dict, mode: Mode) -> dict[str, Any]:
    """
    Run the provider setup step.

    Returns a dict with keys:
        base_url, model, api_key_env, max_tokens, temperature
    Raises GoBack if the user wants to return to the previous step.
    """
    import questionary
    from .helpers import QSTYLE

    if mode == "quickstart":
        section("Step 1 — Your AI Brain")
        c.print("Pick the AI service that will power TinyCTX.\n")
        base_url, api_key_env, provider_name = configure_provider_quickstart(beginner_providers, "LLM")
        from .helpers import pick_model_beginner
        info = beginner_providers[provider_name]
        model = pick_model_beginner(provider_name, base_url, api_key_env, info.get("suggested_models", []))
        max_tokens, temperature = 4096, 1.0
    else:
        section("Step 1 — LLM Provider & Model")
        base_url, api_key_env, provider_name = configure_provider(providers, "LLM", mode)
        from .helpers import pick_model
        model = pick_model(base_url, api_key_env, label="model")
        if mode == "advanced":
            max_tokens  = int(questionary.text("max_tokens",  default="4096", style=QSTYLE).ask() or "4096")
            temperature = float(questionary.text("temperature", default="1",   style=QSTYLE).ask() or "1")
        else:
            max_tokens, temperature = 4096, 1.0

    if not model:
        sys.exit(0)

    success(f"LLM: [bold]{model}[/] via {provider_name}")
    return {
        "base_url":    base_url,
        "model":       model,
        "api_key_env": api_key_env,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }


def run_embeddings(providers: dict, mode: Mode) -> dict[str, Any] | None:
    """
    Optionally configure an embeddings provider.
    Quickstart always skips (BM25-only).
    Returns a config dict, or None to skip.
    Raises GoBack if the user wants to return.
    """
    from .helpers import pick_model

    if mode == "quickstart":
        return None

    section("Step 1b — Embedding Model (optional)")
    c.print("Enables hybrid BM25 + vector memory search. Skip for BM25-only mode.\n")

    raw = input("  Configure an embedding model? (y/n/back, default n): ").strip().lower()
    if raw in ("back", "b"):
        raise GoBack
    if raw not in ("y", "yes"):
        warn("Skipping embeddings — BM25-only memory search will be used.")
        return None

    base_url, api_key_env, provider_name = configure_provider(providers, "Embedding", mode)
    model = pick_model(base_url, api_key_env, label="embedding model")
    if not model:
        sys.exit(0)

    success(f"Embedding: [bold]{model}[/] via {provider_name}")
    return {
        "kind":        "embedding",
        "base_url":    base_url,
        "api_key_env": api_key_env,
        "model":       model,
    }

"""
onboard/providers_setup.py — Step 1: LLM provider & model selection.

Handles both beginner (quickstart) and pro (standard/advanced) flows.
All provider-picking, API-key prompting, model-listing, and model-picking
logic lives here — no external providers.py module required.

Returns provider config dicts ready to be merged into config.yaml.
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Any

import questionary

from .helpers import (
    GoBack,
    LOCAL_PROVIDERS,
    Mode,
    QSTYLE,
    api_key_env_for,
    c,
    fetch_models,
    is_valid_url,
    section,
    set_env,
    success,
    warn,
)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points (called by __main__.py)
# ─────────────────────────────────────────────────────────────────────────────

def run(providers: dict, beginner_providers: dict, mode: Mode) -> dict[str, Any]:
    """
    Run the provider setup step.

    Returns a dict with keys:
        base_url, model, api_key_env, max_tokens, temperature
    Raises GoBack if the user wants to return to the previous step.
    """
    if mode == "quickstart":
        section("Step 1 — Your AI Brain")
        c.print("Pick the AI service that will power TinyCTX.\n")
        base_url, api_key_env, provider_name = _pick_provider_quickstart(beginner_providers, "LLM")
        info  = beginner_providers[provider_name]
        model = _pick_model_beginner(provider_name, base_url, api_key_env, info.get("suggested_models", []))
        max_tokens, temperature = 4096, 1.0
    else:
        section("Step 1 — LLM Provider & Model")
        base_url, api_key_env, provider_name = _pick_provider(providers, "LLM", mode)
        model = _pick_model(base_url, api_key_env, label="model")
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

    base_url, api_key_env, provider_name = _pick_provider(providers, "Embedding", mode)
    model = _pick_model(base_url, api_key_env, label="embedding model")
    if not model:
        sys.exit(0)

    success(f"Embedding: [bold]{model}[/] via {provider_name}")
    return {
        "kind":        "embedding",
        "base_url":    base_url,
        "api_key_env": api_key_env,
        "model":       model,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Provider picking
# ─────────────────────────────────────────────────────────────────────────────

def _pick_provider(
    providers: dict[str, str],
    label: str,
    mode: Mode,
) -> tuple[str, str, str]:
    """
    Standard / advanced provider selector.
    Returns (base_url, api_key_env, provider_name).  Raises GoBack.
    """
    names = sorted(providers.keys()) + ["Custom", "← Back"]

    while True:
        choice = questionary.select(
            f"Select {label} provider:",
            choices=names,
            style=QSTYLE,
        ).ask()

        if choice is None or choice == "← Back":
            raise GoBack

        if choice == "Custom":
            raw = input("  Enter base URL (e.g. http://localhost:8000/v1): ").strip()
            if not raw or raw.lower() in ("back", "b"):
                raise GoBack
            if not is_valid_url(raw):
                warn("That doesn't look like a valid URL. Try again.")
                continue
            base_url      = raw
            provider_name = "Custom"
        else:
            provider_name = choice
            base_url      = providers[choice]

        api_key_env = _ensure_api_key(provider_name, mode)
        return base_url, api_key_env, provider_name


def _pick_provider_quickstart(
    beginner_providers: dict[str, dict],
    label: str,
) -> tuple[str, str, str]:
    """
    Beginner provider selector with guided API-key setup.
    Returns (base_url, api_key_env, provider_name).  Raises GoBack.
    """
    from rich.panel import Panel

    names = list(beginner_providers.keys()) + ["← Back"]

    while True:
        choice = questionary.select(
            f"Which service should power your {label}?",
            choices=names,
            style=QSTYLE,
        ).ask()

        if choice is None or choice == "← Back":
            raise GoBack

        info          = beginner_providers[choice]
        base_url      = info["base_url"]
        provider_name = choice

        # Local providers (e.g. Ollama) need no key
        if not info.get("key_url"):
            steps = info.get("key_steps", [])
            c.print(Panel(
                "\n".join(f"  {i+1}. {step}" for i, step in enumerate(steps)),
                title=f"[bold cyan]Setting up {provider_name}[/]",
                border_style="cyan",
            ))
            input("  Press Enter when ready… ")
            return base_url, "N/A", provider_name

        api_key_env = api_key_env_for(provider_name)
        if os.environ.get(api_key_env, "").strip():
            success(f"{api_key_env} is already set.")
            return base_url, api_key_env, provider_name

        # Guided key setup
        steps = info.get("key_steps", [])
        c.print(Panel(
            "\n".join(f"  {i+1}. {step}" for i, step in enumerate(steps)),
            title=f"[bold cyan]Getting your {provider_name} API key[/]",
            border_style="cyan",
        ))

        while True:
            try:
                raw = getpass.getpass(f"\n  Paste your {provider_name} API key: ").strip()
            except (KeyboardInterrupt, EOFError):
                raw = ""

            if not raw:
                warn("No key entered. You can set it later — but the connection test will be skipped.")
                return base_url, api_key_env, provider_name

            os.environ[api_key_env] = raw
            try:
                set_env(api_key_env, raw)
                success(f"{api_key_env} saved.")
            except Exception as e:
                warn(f"Could not persist {api_key_env} permanently ({e}) — set it manually if needed.")

            c.print("  Testing connection…", end=" ", flush=True)
            models = fetch_models(base_url, api_key_env, timeout=8.0)
            if models:
                c.print("[bold green]OK ✓[/]")
                return base_url, api_key_env, provider_name

            c.print("[bold red]failed ✗[/]")
            warn("Could not reach the API. The key may be wrong or the service may be down.")
            retry = input("  Try a different key? (y/n, default y): ").strip().lower()
            if retry in ("n", "no"):
                warn("Continuing without a verified key.")
                return base_url, api_key_env, provider_name
            # Clear and loop
            os.environ.pop(api_key_env, None)


# ─────────────────────────────────────────────────────────────────────────────
# API-key helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_api_key(provider_name: str, mode: Mode) -> str:
    """
    Check / prompt for an API key for standard / advanced mode.
    Returns the env-var name to store in config.
    """
    if provider_name in LOCAL_PROVIDERS or provider_name == "Custom":
        return "N/A"

    api_key_env = api_key_env_for(provider_name)
    if os.environ.get(api_key_env, "").strip():
        success(f"{api_key_env} is already set.")
        return api_key_env

    c.print(f"\n  [bold yellow]![/] {api_key_env} is not set.")
    c.print(f"  You need an API key from [bold]{provider_name}[/].\n")

    try:
        raw = getpass.getpass(f"  Paste your {provider_name} API key (Enter to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        raw = ""

    if raw:
        os.environ[api_key_env] = raw
        try:
            set_env(api_key_env, raw)
            success(f"{api_key_env} saved.")
        except Exception as e:
            warn(f"Could not persist {api_key_env} permanently ({e}) — set it manually before restarting.")
    else:
        warn(f"{api_key_env} not set — you can set it later before starting TinyCTX.")

    return api_key_env


# ─────────────────────────────────────────────────────────────────────────────
# Model picking
# ─────────────────────────────────────────────────────────────────────────────

def _pick_model(base_url: str, api_key_env: str, label: str = "model") -> str:
    """
    Fetch the model list from the provider and let the user pick one.
    Falls back to a free-text prompt if the list can't be fetched.
    Returns the chosen model string (never empty — exits if user aborts).
    """
    c.print(f"  Fetching {label} list…", end=" ", flush=True)
    models = fetch_models(base_url, api_key_env)

    if models:
        c.print(f"[dim]({len(models)} found)[/]")
        choices = models + ["✏  Enter manually", "← Back"]
        choice = questionary.select(
            f"Select {label}:",
            choices=choices,
            style=QSTYLE,
        ).ask()

        if choice is None or choice == "← Back":
            raise GoBack
        if choice != "✏  Enter manually":
            return choice
    else:
        c.print("[dim](could not fetch list)[/]")

    # Free-text fallback
    while True:
        raw = input(f"  Type the {label} name (or 'back'): ").strip()
        if raw.lower() in ("back", "b"):
            raise GoBack
        if raw:
            return raw
        warn("Model name cannot be empty.")


def _pick_model_beginner(
    provider_name: str,
    base_url: str,
    api_key_env: str,
    suggested_models: list[str],
) -> str:
    """
    Beginner model picker: show curated suggestions first, then offer
    the full live list or manual entry as a fallback.
    Returns the chosen model string.
    """
    choices: list[str] = list(suggested_models)

    # Try to fetch the live list and surface any models not already in suggestions
    live = fetch_models(base_url, api_key_env, timeout=6.0)
    extras = [m for m in live if m not in choices]
    if extras:
        choices += ["── more models ──"] + extras  # visual separator (non-selectable label)

    choices += ["✏  Enter manually", "← Back"]

    while True:
        choice = questionary.select(
            f"Which {provider_name} model would you like to use?",
            choices=choices,
            style=QSTYLE,
        ).ask()

        if choice is None or choice == "← Back":
            raise GoBack
        if choice == "── more models ──":
            warn("That's a separator — please pick a model above or below it.")
            continue
        if choice == "✏  Enter manually":
            raw = input("  Type the model name: ").strip()
            if raw:
                return raw
            warn("Model name cannot be empty.")
            continue
        return choice

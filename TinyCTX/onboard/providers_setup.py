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
    base_url_fix,
    section,
    set_env,
    success,
    warn,
    is_local
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
        base_url, api_key_env, provider_name = _pick_provider(beginner_providers, "LLM", mode)
    else:
        section("Step 1 — LLM Provider & Model")
        base_url, api_key_env, provider_name = _pick_provider(providers, "LLM", mode)

    model = _pick_model(base_url, api_key_env, label="model")      
    max_tokens, temperature = 4096, 1.0

    if not model:
        sys.exit(0)

    context = _pick_context(mode)

    success(f"LLM: [bold]{model}[/] via {provider_name}")
    return {
        "base_url":    base_url,
        "model":       model,
        "api_key_env": api_key_env,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "context":     context,
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

    want = questionary.select(
        "Configure an embedding model?",
        choices=["No (BM25-only)", "Yes", "← Back"],
        default="No (BM25-only)",
        style=QSTYLE,
    ).ask()

    if want is None or want == "← Back":
        raise GoBack
    if want != "Yes":
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
    names = ["← Back", "Custom"] + sorted(providers.keys())

    while True:
        choice = questionary.select(
            f"Select {label} provider:",
            choices=names,
            style=QSTYLE,
        ).ask()

        if choice is None or choice == "← Back":
            raise GoBack

        if choice == "Custom":
            raw = questionary.text("Enter base URL:", style=QSTYLE).ask()
            if not raw or raw.strip().lower() in ("back", "b"): continue
            base_url, provider_name = raw.strip(), "Custom"
            base_url = base_url_fix(base_url)
            if not is_valid_url(base_url):
                warn("Invalid URL."); continue
        else:
            provider_name, base_url = choice, providers[choice]

        # ── Autodetect Auth Requirement ──────────────────────────────────────
        c.print(f"  Probing {provider_name}...", end=" ")
        
        # Probe 1: Try without a key
        probe_models = fetch_models(base_url, None)
        
        if probe_models is not None:
            # Success! No key needed (returned a list, even if empty)
            c.print("[bold green]Open ✓[/]")
            return base_url, "N/A", provider_name
        
        # Probe failed with 401/403 (None) -> Key is required
        api_key_env = _ensure_api_key(provider_name, mode)

        # Final connection test with the key
        api_key = os.environ.get(api_key_env, "")
        models = fetch_models(base_url, api_key)

        if models:
            c.print(f"  [bold green]Connected ✓[/] [dim]({len(models)} models)[/]")
            return base_url, api_key_env, provider_name

        # ── Failure Handling ────────────────────────────────────────────────
        c.print("[bold red]failed ✗[/]")
        locality = provider_name in LOCAL_PROVIDERS or is_local(base_url)
        if locality:
            warn(f"Could not reach {base_url}. Ensure the server is running.")
            continue

        action = questionary.select(
            "What would you like to do?",
            choices=["Re-enter API key", "Choose a different provider", "← Back"],
            style=QSTYLE,
        ).ask()

        if action in (None, "← Back"): raise GoBack
        if action == "Choose a different provider":
            os.environ.pop(api_key_env, None)
            continue
        
        os.environ.pop(api_key_env, None)
        # Loop will re-prompt for key

# ─────────────────────────────────────────────────────────────────────────────
# Model picking
# ─────────────────────────────────────────────────────────────────────────────

def _pick_model(base_url: str, api_key_env: str, label: str = "model") -> str:
    c.print(f"  Fetching {label} list…", end=" ")
    api_key = os.environ.get(api_key_env) if api_key_env != "N/A" else None
    models = fetch_models(base_url, api_key)

    if models:
        c.print(f"[dim]({len(models)} found)[/]")
        shown = models[:20]
        truncated = len(models) > 20

        choices = shown + (
            ["… (truncated, type manually for more)"] if truncated else []
        ) + ["✏  Enter manually", "← Back"]

        choice = questionary.select(
            f"Select {label}:",
            choices=choices,
            style=QSTYLE,
        ).ask()

        if choice is None or choice == "← Back":
            raise GoBack
        if choice not in ("✏  Enter manually", "… (truncated, type manually for more)"):
            return choice
    else:
        c.print("[dim](could not fetch list)[/]")

    # Free-text fallback + validation
    model_set = set(models or [])

    while True:
        raw = questionary.text(f"  Type the {label} name:", style=QSTYLE).ask()
        if not raw or raw.strip().lower() in ("back", "b"):
            raise GoBack

        raw = raw.strip()
        if not raw:
            warn("Model name cannot be empty.")
            continue

        if model_set and raw not in model_set:
            warn("Model not found in registry. Check spelling or pick from list.")
            continue

        return raw

# ─────────────────────────────────────────────────────────────────────────────
# Context window
# ─────────────────────────────────────────────────────────────────────────────

_CONTEXT_PRESETS = [
    ("16k — 16,384  (default)",              16384),
    ("32k — 32,768",                         32768),
    ("64k — 65,536",                         65536),
    ("128k — 131,072",                      131072),
    ("✏  Enter manually",                      None),
]

def _validate_context(val: int) -> int | None:
    """
    Warn on suspicious context sizes.
    Returns the value if confirmed, or None to signal the user wants to re-enter.
    """
    if val > 1_000_000:
        if not _confirm_context(
            f"{val:,} tokens is over 1 million — are you sure your model supports this??????"
        ):
            return None

    if val < 16384:
        if not _confirm_context(
            f"Context of {val:,} tokens is below 16k — TinyCTX may not work properly "
            "(system prompt + memory injection can easily exceed this)."
        ):
            return None

    if val % 2 != 0:
        if not _confirm_context(
            f"{val:,} is an odd number — are you sure your model supports this context size?"
        ):
            return None

    return val


def _confirm_context(warning: str) -> bool:
    """Show a warning and force the user to explicitly confirm or go back."""
    warn(warning)
    choice = questionary.select(
        "How would you like to proceed?",
        choices=["Yes, I'm sure this is what I want.", "No, let me change that."],
        style=QSTYLE,
    ).ask()
    return choice is not None and choice.startswith("Yes")


def _pick_context(mode: Mode) -> int:
    """Ask for context window size in standard mode; return default in quickstart."""
    if mode == "quickstart":
        return 16384

    c.print()
    c.print("  Context window — how many tokens the model can see per turn.")
    c.print("  Check your model's spec if unsure.\n")

    labels  = [label for label, _ in _CONTEXT_PRESETS]
    values  = {label: val for label, val in _CONTEXT_PRESETS}

    choice = questionary.select(
        "Context window size:",
        choices=labels,
        default="16k — 16,384  (default)",
        style=QSTYLE,
    ).ask()

    if choice is None:
        return 16384

    if values[choice] is not None:
        result = _validate_context(values[choice])
        if result is not None:
            return result
        # User said "No, let me change that" -- drop back to preset menu
        return _pick_context(mode)

    # Manual entry
    while True:
        raw = questionary.text("  Enter context size (tokens):", style=QSTYLE).ask()
        if not raw:
            return 16384
        try:
            val = int(raw.strip().replace(",", "").replace("_", ""))
            if val < 512:
                warn("Context size seems too small (minimum 512).")
                continue
            result = _validate_context(val)
            if result is not None:
                return result
            # User said "No, let me change that" -- re-prompt
        except ValueError:
            warn(f"'{raw}' is not a valid integer.")


# ─────────────────────────────────────────────────────────────────────────────
# API-key helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_api_key(provider_name: str, mode: Mode) -> str:
    """
    Check / prompt for an API key for standard / advanced mode.
    Returns the env-var name to store in config.
    """
    if provider_name in LOCAL_PROVIDERS:
        return "N/A"

    api_key_env = api_key_env_for(provider_name)
    if os.environ.get(api_key_env, "").strip():
        success(f"{api_key_env} is already set.")
        return api_key_env

    c.print(f"\n  [bold yellow]![/] {api_key_env} is not set.")
    c.print(f"  You need an API key from [bold]{provider_name}[/].\n")

    while True:
        try:
            raw = getpass.getpass(f"  Paste your {provider_name} API key (or press Enter to skip): ").strip()
        except (KeyboardInterrupt, EOFError):
            raw = ""

        if not raw:
            warn(f"{api_key_env} not set — some providers may require a API key.")
            return api_key_env

        os.environ[api_key_env] = raw
        try:
            set_env(api_key_env, raw)
            success(f"{api_key_env} saved.")
        except Exception as e:
            warn(f"Could not persist {api_key_env} permanently ({e}) — set it manually before restarting.")
        return api_key_env
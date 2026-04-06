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
        base_url, api_key_env, provider_name = _pick_provider_quickstart(beginner_providers, "LLM")
        info  = beginner_providers[provider_name]
        model = _pick_model_beginner(provider_name, base_url, api_key_env, info.get("suggested_models", []))
    else:
        section("Step 1 — LLM Provider & Model")
        base_url, api_key_env, provider_name = _pick_provider(providers, "LLM", mode)
        model = _pick_model(base_url, api_key_env, label="model")
        
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
    """
    Standard / advanced provider selector.

    After picking a provider and entering a key, performs a live connection
    test. If the test fails the user is told why and must either retry their
    key or choose a different provider — they cannot proceed with a broken
    provider.

    Returns (base_url, api_key_env, provider_name).  Raises GoBack.
    """
    names =  ["← Back", "Custom"] + sorted(providers.keys())

    while True:
        choice = questionary.select(
            f"Select {label} provider:",
            choices=names,
            style=QSTYLE,
        ).ask()

        if choice is None or choice == "← Back":
            raise GoBack

        if choice == "Custom":
            raw = questionary.text(
                "Enter base URL (e.g. http://localhost:8000/v1):",
                style=QSTYLE,
            ).ask()
            if not raw or raw.strip().lower() in ("back", "b"):
                raise GoBack
            raw = raw.strip()
            if not is_valid_url(raw):
                warn("That doesn't look like a valid URL. Try again.")
                continue
            base_url      = raw
            provider_name = "Custom"
        else:
            provider_name = choice
            base_url      = providers[choice]

        api_key_env = _ensure_api_key(provider_name, mode)

        # ── Connection test ───────────────────────────────────────────────
        locality = provider_name in LOCAL_PROVIDERS or is_local(base_url)
        c.print("  Testing connection…", end=" ")
        models = fetch_models(base_url, api_key_env, timeout=8.0)

        if models:
            c.print(f"[bold green]OK ✓[/] [dim]({len(models)} models)[/]")
            return base_url, api_key_env, provider_name

        c.print("[bold red]failed ✗[/]")

        if locality:
            warn(
                f"Could not reach {base_url}. "
                "Make sure the server is running, then try again."
            )
            # Loop back to provider selection
            continue

        # Remote provider — key is likely wrong or service is down
        warn("Could not reach the API. The key may be invalid or the service may be down.")
        action = questionary.select(
            "What would you like to do?",
            choices=[
                "Re-enter API key",
                "Choose a different provider",
                "← Back",
            ],
            style=QSTYLE,
        ).ask()

        if action is None or action == "← Back":
            raise GoBack
        if action == "Choose a different provider":
            # Clear the bad key so _ensure_api_key will prompt again
            os.environ.pop(api_key_env, None)
            continue

        # Re-enter key: clear and re-run _ensure_api_key inline
        os.environ.pop(api_key_env, None)
        api_key_env = _ensure_api_key(provider_name, mode)
        # Loop will re-test on next iteration


# ─────────────────────────────────────────────────────────────────────────────
# Model picking
# ─────────────────────────────────────────────────────────────────────────────

def _pick_model(base_url: str, api_key_env: str, label: str = "model") -> str:
    c.print(f"  Fetching {label} list…", end=" ")
    models = fetch_models(base_url, api_key_env)

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

    if not choices:
        return _pick_model(base_url, api_key_env, label="model")

    choices += ["Show all available models", "← Back"]

    choice = questionary.select(
        f"Which {provider_name} model would you like to use?",
        choices=choices,
        style=QSTYLE,
    ).ask()

    if choice is None or choice == "← Back":
        raise GoBack
    if choice == "Show all available models":
        return _pick_model(base_url, api_key_env, label="model")
    return choice


def _pick_provider_quickstart(
    beginner_providers: dict[str, dict],
    label: str,
) -> tuple[str, str, str]:
    """
    Beginner provider selector with guided API-key setup and mandatory
    connection test.
    Returns (base_url, api_key_env, provider_name).  Raises GoBack.
    """
    from rich.panel import Panel

    names = ["← Back"] + list(beginner_providers.keys())

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

        # Local providers (e.g. Ollama) need no key — test and loop on failure
        if not info.get("key_url"):
            steps = info.get("key_steps", [])
            if steps:
                c.print(Panel(
                    "\n".join(f"  {i+1}. {step}" for i, step in enumerate(steps)),
                    title=f"[bold cyan]Setting up {provider_name}[/]",
                    border_style="cyan",
                ))
                input("  Press Enter when ready… ")

            c.print("  Testing connection…", end=" ")
            models = fetch_models(base_url, "N/A", timeout=8.0)
            if models:
                c.print(f"[bold green]OK ✓[/] [dim]({len(models)} models)[/]")
                return base_url, "N/A", provider_name

            c.print("[bold red]failed ✗[/]")
            warn(f"Could not reach {base_url}. Make sure {provider_name} is running.")
            retry = questionary.select(
                "What would you like to do?",
                choices=["Try again", "Choose a different provider", "← Back"],
                style=QSTYLE,
            ).ask()
            if retry is None or retry == "← Back":
                raise GoBack
            if retry == "Try again":
                # Re-show the setup panel and test again
                continue
            # Choose a different provider — loop to top
            continue

        api_key_env = api_key_env_for(provider_name)
        if os.environ.get(api_key_env, "").strip():
            success(f"{api_key_env} is already set.")
            c.print("  Testing connection…", end=" ")
            models = fetch_models(base_url, api_key_env, timeout=8.0)
            if models:
                c.print(f"[bold green]OK ✓[/] [dim]({len(models)} models)[/]")
                return base_url, api_key_env, provider_name
            c.print("[bold red]failed ✗[/]")
            warn("Existing key didn't work. Let's re-enter it.")
            os.environ.pop(api_key_env, None)

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
                warn("No key entered. Returning to provider selection.")
                break  # back to outer while to re-pick provider

            os.environ[api_key_env] = raw
            try:
                set_env(api_key_env, raw)
                success(f"{api_key_env} saved.")
            except Exception as e:
                warn(f"Could not persist {api_key_env} permanently ({e}) — set it manually if needed.")

            c.print("  Testing connection…", end=" ")
            models = fetch_models(base_url, api_key_env, timeout=8.0)
            if models:
                c.print(f"[bold green]OK ✓[/] [dim]({len(models)} models)[/]")
                return base_url, api_key_env, provider_name

            c.print("[bold red]failed ✗[/]")
            warn("Could not reach the API. The key may be wrong or the service may be down.")
            action = questionary.select(
                "What would you like to do?",
                choices=["Re-enter API key", "Choose a different provider", "← Back"],
                style=QSTYLE,
            ).ask()
            if action is None or action == "← Back":
                raise GoBack
            if action == "Choose a different provider":
                os.environ.pop(api_key_env, None)
                break  # back to outer while
            # Re-enter key — clear and loop inner while
            os.environ.pop(api_key_env, None)


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
            warn(f"{api_key_env} not set — connection test will be skipped.")
            return api_key_env

        os.environ[api_key_env] = raw
        try:
            set_env(api_key_env, raw)
            success(f"{api_key_env} saved.")
        except Exception as e:
            warn(f"Could not persist {api_key_env} permanently ({e}) — set it manually before restarting.")
        return api_key_env

"""
onboard/providers.py — provider selection prompts for each setup mode.

Each function returns (base_url, api_key_env, provider_name).
"""

from __future__ import annotations

import getpass
import os
import sys

import questionary
from rich.panel import Panel

from .helpers import (
    LOCAL_PROVIDERS,
    QSTYLE,
    Mode,
    api_key_env_for,
    c,
    is_valid_url,
    set_env,
    success,
    warn,
)


def _prompt_secret(prompt: str) -> str:
    """Prompt for a secret value without echo. Returns stripped string or '' if blank."""
    try:
        value = getpass.getpass(prompt + ": ")
        return value.strip() if value else ""
    except (KeyboardInterrupt, EOFError):
        return ""


def configure_provider_quickstart(beginner_providers: dict[str, dict], label: str) -> tuple[str, str, str]:
    """
    Quickstart flow: pick from the curated beginner list and show an inline
    API-key guide so the user knows exactly what to do next.
    """
    provider_name = questionary.select(
        "Which AI service do you want to use?",
        choices=list(beginner_providers.keys()),
        style=QSTYLE,
    ).ask()
    if provider_name is None:
        sys.exit(0)

    info     = beginner_providers[provider_name]
    base_url = info["base_url"]

    steps_text = "\n".join(f"  {i+1}. {step}" for i, step in enumerate(info["key_steps"]))

    if provider_name in LOCAL_PROVIDERS or info.get("key_url") is None:
        api_key_env = "N/A"
        c.print()
        c.print(Panel(steps_text, title=f"[bold cyan]Setting up {provider_name}[/]", border_style="cyan"))
    else:
        api_key_env  = api_key_env_for(provider_name)
        export_cmd   = "set" if __import__("sys").platform == "win32" else "export"
        c.print()
        c.print(Panel(
            f"{steps_text}\n\n  Then run:\n  [bold]{export_cmd} {api_key_env}=your-key-here[/]",
            title=f"[bold cyan]How to get your {provider_name} API key[/]",
            border_style="cyan",
        ))
        if not os.environ.get(api_key_env):
            c.print()
            entered = _prompt_secret(f"Paste your {provider_name} API key (or leave blank to set it later)")
            if entered:
                os.environ[api_key_env] = entered
                try:
                    set_env(api_key_env, entered)
                    success(f"{api_key_env} saved to your shell profile and set for this session.")
                except Exception as e:
                    warn(f"Could not persist {api_key_env} permanently ({e}) — set it manually before restarting.")
            else:
                warn(f"{api_key_env} not set — you'll need it before starting the agent.")
        else:
            success(f"{api_key_env} is already set.")

    return base_url, api_key_env, provider_name


def configure_provider(providers: dict[str, str], label: str, mode: Mode) -> tuple[str, str, str]:
    """
    Standard / Advanced flow: full provider list with an optional Custom entry.
    Advanced puts Custom at the top; Standard appends it at the bottom.
    """
    if mode == "advanced":
        provider_choices = ["Custom"] + list(providers.keys())
    else:
        provider_choices = list(providers.keys()) + ["Custom"]

    provider_name = questionary.select(
        f"{label} provider",
        choices=provider_choices,
        style=QSTYLE,
    ).ask()
    if provider_name is None:
        sys.exit(0)

    if provider_name == "Custom":
        while True:
            raw_url = questionary.text(
                "Base URL (e.g. http://localhost:8000/v1)",
                style=QSTYLE,
            ).ask() or ""
            if is_valid_url(raw_url):
                base_url = raw_url.rstrip("/")
                break
            warn(f"'{raw_url}' doesn't look like a valid URL — must start with http:// or https://")
        raw_key = input("  Requires an API key? (y/n, default n): ").strip().lower()
        use_key = raw_key in ("y", "yes")
        api_key_env = (
            questionary.text("Env var name for API key", default="CUSTOM_API_KEY", style=QSTYLE).ask()
            or "CUSTOM_API_KEY"
        ) if use_key else "N/A"
    else:
        base_url    = providers[provider_name]
        api_key_env = "N/A" if provider_name in LOCAL_PROVIDERS else api_key_env_for(provider_name)
        if api_key_env != "N/A" and not os.environ.get(api_key_env):
            c.print()
            entered = _prompt_secret(f"Paste your {provider_name} API key (or leave blank to set it later)")
            if entered:
                os.environ[api_key_env] = entered
                try:
                    set_env(api_key_env, entered)
                    success(f"{api_key_env} saved to your shell profile and set for this session.")
                except Exception as e:
                    warn(f"Could not persist {api_key_env} permanently ({e}) — set it manually before restarting.")
            else:
                warn(f"{api_key_env} not set — set it before starting the agent.")

    return base_url, api_key_env, provider_name

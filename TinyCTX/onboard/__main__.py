"""
TinyCTX Onboarding Wizard
Run with: python -m onboard          (from repo root)
          python -m onboard --reset
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import questionary
from rich.markdown import Markdown
from rich.panel import Panel

from .helpers import (
    BANNER,
    CONFIG_PATH,
    GoBack,
    Mode,
    QSTYLE,
    assemble_config,
    c,
    load_beginner_providers,
    load_existing_config,
    load_providers,
    section,
    success,
    warn,
    write_config,
)
from . import providers_setup, bridges_setup, workspace_setup, gateway_setup


# ── Step 0a: detect existing config ──────────────────────────────────────────

def step_detect(reset: bool) -> str:
    """Returns 'new' | 'modify' | 'reset'."""
    if not CONFIG_PATH.exists():
        return "new"

    section("Existing Configuration Detected")
    c.print(f"Found [bold]{CONFIG_PATH}[/]")

    if load_existing_config() is None:
        c.print("[bold red]✗[/] Config is invalid — run [bold]python -m onboard --reset[/] to start fresh.")
        sys.exit(1)

    if reset:
        return "reset"

    choice = questionary.select(
        "What would you like to do?",
        choices=[
            "Keep existing config (exit)",
            "Modify (update specific sections)",
            "Reset (start from scratch)",
        ],
        default="Modify (update specific sections)",
        style=QSTYLE,
    ).ask()

    if choice is None or "Keep" in choice:
        success("Nothing changed.")
        sys.exit(0)

    return "reset" if "Reset" in choice else "modify"


# ── Step 0b: beginner or pro? ─────────────────────────────────────────────────

def step_select_mode() -> Mode:
    section("Setup Mode")
    c.print("Choose how much you want to configure:\n")

    choice = questionary.select(
        "Which setup experience would you like?",
        choices=[
            "🟢  Quick Start  — I'm new to AI agents, just get me going",
            "🟡  Standard     — I know what I'm doing",
            "← Back",
        ],
        style=QSTYLE,
    ).ask()

    if choice is None:
        sys.exit(0)
    if "Back" in choice:
        raise GoBack

    if "Quick Start" in choice:
        c.print("\n[bold green]Quick Start[/] selected — we'll keep things simple!\n")
        return "quickstart"
    elif "Standard" in choice:
        c.print("\n[bold yellow]Standard[/] selected.\n")
        return "standard"
    else:
        c.print("\n[bold red]Advanced[/] selected — buckle up.\n")
        return "advanced"


# ── optional: advanced agent settings ─────────────────────────────────────────

def step_max_tool_cycles(mode: Mode) -> int:
    if mode != "advanced":
        return 25
    section("Agent Settings")
    c.print("Max tool cycles: how many tool calls the agent can make per turn before stopping.\n")
    return int(questionary.text("max_tool_cycles", default="25", style=QSTYLE).ask() or "25")


# ── final: summary ─────────────────────────────────────────────────────────────

def step_summary(mode: Mode, gateway: dict[str, Any]) -> None:
    section("Done!")
    host, port, key = gateway["host"], gateway["port"], gateway["api_key"]

    if mode == "quickstart":
        body = Markdown(f"""
**Config written to:** `{CONFIG_PATH}`

### Next steps

1. **Set your API key** if you haven't already:
   - Windows: `set YOUR_PROVIDER_API_KEY=sk-...`
   - Mac/Linux: `export YOUR_PROVIDER_API_KEY=sk-...`

2. **Start TinyCTX:**
   ```
   python -m main
   ```

3. **Connect a client** (e.g. SillyTavern) to:
   - URL: `http://{host}:{port}`
   - API Key: `{key}`

That's it! Re-run `python -m onboard` at any time to reconfigure.
""")
        c.print(Panel(body, title="[bold green]TinyCTX is ready 🎉[/]", border_style="green"))

    else:
        body = Markdown(f"""
**Config written to:** `{CONFIG_PATH}`

**Next steps:**

1. Set any missing environment variables (API keys, bot tokens).
2. Start the agent: `python -m main`
3. Gateway: `http://{host}:{port}`  |  Key: `{key}`
4. To reconfigure: re-run `python -m onboard`
""")
        title = "[bold green]TinyCTX is ready[/]"
        if mode == "advanced":
            body = Markdown(str(body.markup) + "\n5. For advanced tuning, edit `config.yaml` directly.")
        c.print(Panel(body, title=title, border_style="green"))


# ── wizard runner (with GoBack support) ──────────────────────────────────────

def run_wizard(providers: dict, beginner_providers: dict, existing: dict | None) -> None:
    """
    Run all wizard steps in order.
    Any step can raise GoBack to return to the previous step.
    """

    # ── Step 0b: mode selection ───────────────────────────────────────────────
    step_idx = 0
    mode: Mode = "quickstart"
    results: dict[str, Any] = {}

    steps = [
        "mode",
        "providers",
        "bridges",
        "workspace",
        "gateway",
    ]

    while step_idx < len(steps):
        current = steps[step_idx]
        try:
            if current == "mode":
                mode = step_select_mode()

            elif current == "providers":
                results["model_cfg"] = providers_setup.run(providers, beginner_providers, mode)
                results["embed_cfg"] = providers_setup.run_embeddings(providers, mode)

            elif current == "bridges":
                results["bridges"] = bridges_setup.run(mode)

            elif current == "workspace":
                results["workspace"] = workspace_setup.run(mode)

            elif current == "gateway":
                # Collect config only — do NOT launch yet (config not written yet)
                results["gateway"] = gateway_setup.run(mode)

            step_idx += 1

        except GoBack:
            if step_idx > 0:
                step_idx -= 1
                c.print("\n[dim]↩  Going back…[/]\n")
            else:
                c.print("\n[dim]Already at the first step.[/]\n")

    # ── Agent settings (advanced only, no GoBack needed) ─────────────────────
    max_tool_cycles = step_max_tool_cycles(mode)

    # ── Write config ──────────────────────────────────────────────────────────
    data = assemble_config(
        results["model_cfg"],
        results.get("embed_cfg"),
        results["workspace"],
        results["gateway"],
        results["bridges"],
        max_tool_cycles,
        existing,
    )
    write_config(data)
    success(f"Config written to [bold]{CONFIG_PATH}[/]")

    # ── Launch gateway AFTER config is on disk ────────────────────────────────
    gateway_setup.launch(results["gateway"])

    step_summary(mode, results["gateway"])


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        prog="python -m onboard",
        description="TinyCTX Onboarding Wizard",
    )
    p.add_argument("--reset", action="store_true", help="Wipe existing config and start fresh")
    args = p.parse_args()

    providers          = load_providers()
    beginner_providers = load_beginner_providers()
    existing           = load_existing_config()

    c.print(f"[bold cyan]{BANNER}[/]")

    detect = step_detect(args.reset)
    if detect == "reset":
        CONFIG_PATH.unlink(missing_ok=True)
        existing = None
        success("Config reset.")
    elif detect == "new":
        c.print(Panel(
            "[bold]Welcome![/] Let's get TinyCTX configured.\n\n"
            "  • Press [bold]Ctrl+C[/] at any time to cancel.\n"
            "  • You can undo your last choice by selecting [bold]← Back[/].",
            border_style="cyan",
        ))

    try:
        run_wizard(providers, beginner_providers, existing)
    except KeyboardInterrupt:
        c.print("\n\n[bold yellow]Onboarding cancelled.[/] Run [bold]python -m onboard[/] to start again.")
        sys.exit(0)


if __name__ == "__main__":
    main()

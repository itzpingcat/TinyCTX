"""
TinyCTX Onboarding Wizard
Run with: python -m onboard  (from repo root)
         python -m onboard --reset
"""

from __future__ import annotations

import secrets
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

import questionary
import yaml
from rich.markdown import Markdown
from rich.panel import Panel

from .helpers import (
    BANNER, BUNDLED_DIR, BUNDLED_MD, CONFIG_PATH,
    DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT, DEFAULT_WORKSPACE,
    GoBack, Mode, QSTYLE,
    assemble_config, c, health_ping, load_beginner_providers,
    load_existing_config, load_providers, pick_model, pick_model_beginner,
    section, success, warn, write_config,
)
from .providers import configure_provider, configure_provider_quickstart


# ── pre-flight ────────────────────────────────────────────────────────────────

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
        choices=["Keep existing config (exit)", "Modify (update specific sections)", "Reset (start from scratch)"],
        default="Modify (update specific sections)",
        style=QSTYLE,
    ).ask()

    if choice is None or "Keep" in choice:
        success("Nothing changed.")
        sys.exit(0)
    return "reset" if "Reset" in choice else "modify"


def step_select_mode() -> Mode:
    section("Setup Mode")
    c.print("Choose how much you want to configure:\n")

    choice = questionary.select(
        "Which setup experience would you like?",
        choices=[
            "🟢  Quick Start  — I'm new, just get me going",
            "🟡  Standard     — I know what I'm doing",
            "🔴  Advanced     — Show me everything",
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


# ── wizard steps ──────────────────────────────────────────────────────────────

def step_llm(providers: dict, beginner_providers: dict, mode: Mode) -> dict[str, Any]:
    section("Step 1 — Your AI Brain" if mode == "quickstart" else "Step 1 — LLM Provider & Model")
    if mode == "quickstart":
        c.print("Pick the AI service that will power TinyCTX.\n")

    if mode == "quickstart":
        base_url, api_key_env, provider_name = configure_provider_quickstart(beginner_providers, "LLM")
        info  = beginner_providers[provider_name]
        model = pick_model_beginner(provider_name, base_url, api_key_env, info.get("suggested_models", []))
        max_tokens, temperature = 4096, 1.0
    else:
        base_url, api_key_env, provider_name = configure_provider(providers, "LLM", mode)
        model = pick_model(base_url, api_key_env, label="model")
        if mode == "advanced":
            max_tokens  = int(questionary.text("max_tokens",  default="4096", style=QSTYLE).ask() or "4096")
            temperature = float(questionary.text("temperature", default="1",   style=QSTYLE).ask() or "1")
        else:
            max_tokens, temperature = 4096, 1.0

    if not model:
        sys.exit(0)

    success(f"Primary: [bold]{model}[/] via {provider_name}")
    return {"base_url": base_url, "model": model, "api_key_env": api_key_env,
            "max_tokens": max_tokens, "temperature": temperature}


def step_embedding(providers: dict, mode: Mode) -> dict[str, Any] | None:
    """Quickstart always skips (BM25-only). Standard/Advanced prompt."""
    if mode == "quickstart":
        return None

    section("Step 2 — Embedding Model (optional)")
    c.print("Enables hybrid BM25+vector memory search. Skip for BM25-only mode.\n")

    raw = input("  Configure an embedding model? (y/n/back, default n): ").strip().lower()
    if raw in ("back", "b"):
        raise GoBack
    if raw not in ("y", "yes"):
        warn("BM25-only memory search will be used.")
        return None

    base_url, api_key_env, provider_name = configure_provider(providers, "Embedding", mode)
    model = pick_model(base_url, api_key_env, label="embedding model")
    if not model:
        sys.exit(0)

    success(f"Embedding: [bold]{model}[/] via {provider_name}")
    return {"kind": "embedding", "base_url": base_url, "api_key_env": api_key_env, "model": model}


def step_workspace(mode: Mode) -> str:
    if mode == "quickstart":
        section("Step 2 — Where to Save Your Data")
        c.print(f"TinyCTX will store your sessions and memory here.\n")
        c.print(f"  Default: [bold]{DEFAULT_WORKSPACE}[/]  (recommended)\n")
        raw = input(f"  Workspace path (Enter for default, 'back' to go back): ").strip()
        if raw.lower() in ("back", "b"):
            raise GoBack
        workspace = raw if raw else DEFAULT_WORKSPACE
    else:
        section("Step 3 — Workspace")
        c.print("Stores sessions, memory index, SOUL.md, AGENTS.md, etc.\n")
        raw = input(f"  Workspace path (default: {DEFAULT_WORKSPACE}, 'back' to go back): ").strip()
        if raw.lower() in ("back", "b"):
            raise GoBack
        workspace = raw if raw else DEFAULT_WORKSPACE

    Path(workspace).expanduser().mkdir(parents=True, exist_ok=True)
    success(f"Workspace: [bold]{Path(workspace).expanduser()}[/]")
    return workspace


def step_gateway(mode: Mode) -> dict[str, Any]:
    if mode == "quickstart":
        section("Step 3 — Access Key")
        c.print("TinyCTX uses a secret key so only you can connect to it.\n")
        raw = input("  Gateway API key (Enter to auto-generate, 'back' to go back): ").strip()
        if raw.lower() in ("back", "b"):
            raise GoBack
        api_key = raw if raw else secrets.token_hex(16)
        success(f"Key: [bold]{api_key}[/]  (save this somewhere!)")
        return {"enabled": True, "host": DEFAULT_GATEWAY_HOST, "port": DEFAULT_GATEWAY_PORT, "api_key": api_key}

    section("Step 4 — Gateway (HTTP/SSE API)")
    c.print("Exposes TinyCTX to SillyTavern, curl, and other external clients.\n")
    raw_host = input(f"  Bind host (default: {DEFAULT_GATEWAY_HOST}, 'back' to go back): ").strip()
    if raw_host.lower() in ("back", "b"):
        raise GoBack
    host = raw_host if raw_host else DEFAULT_GATEWAY_HOST
    while True:
        raw_port = input(f"  Port (default: {DEFAULT_GATEWAY_PORT}): ").strip()
        if not raw_port:
            port = DEFAULT_GATEWAY_PORT
            break
        try:
            port = int(raw_port)
            if 1 <= port <= 65535:
                break
            warn(f"Port must be between 1 and 65535.")
        except ValueError:
            warn(f"'{raw_port}' is not a valid port number.")
    raw_key = input("  API key (Enter to auto-generate): ").strip()
    api_key = raw_key if raw_key else secrets.token_hex(16)
    success(f"Gateway: http://{host}:{port}  key=[bold]{api_key}[/]")
    return {"enabled": True, "host": host, "port": port, "api_key": api_key}


def step_bridges(mode: Mode) -> dict[str, Any]:
    if mode == "quickstart":
        section("Step 4 — Connect to Platforms")
        c.print("TinyCTX can run as a bot on Discord or Matrix.\n")
    else:
        section("Step 5 — Bridges")
        c.print("CLI is always enabled. Select additional bridges:\n")

    available = ["Discord", "Matrix"]
    c.print("  Available: " + ", ".join(f"[bold]{b}[/]" for b in available))
    c.print("  Type the names you want, comma-separated (or leave blank for none, 'back' to go back).")
    raw = input("  Bridges: ").strip()
    if raw.lower() in ("back", "b"):
        raise GoBack
    chosen = {x.strip().title() for x in raw.split(",") if x.strip()} if raw else set()
    chosen = {b for b in chosen if b in available}

    bridges: dict[str, Any] = {"cli": {"enabled": True}}

    if "Discord" in chosen:
        _setup_discord_bridge(bridges, mode)
    if "Matrix" in chosen:
        _setup_matrix_bridge(bridges, mode)

    return bridges


def _setup_discord_bridge(bridges: dict, mode: Mode) -> None:
    import getpass
    import os
    from .helpers import set_env

    def _prompt_secret(prompt: str) -> str:
        try:
            v = getpass.getpass(prompt + ": ")
            return v.strip() if v else ""
        except (KeyboardInterrupt, EOFError):
            return ""
    c.print()
    c.print(Panel(
        "  1. Go to [bold]discord.com/developers/applications[/]\n"
        "  2. Click [bold]New Application[/], give it a name\n"
        "  3. Go to [bold]Bot[/] → click [bold]Add Bot[/]\n"
        "  4. Under [bold]Privileged Gateway Intents[/], turn on:\n"
        "       • Message Content Intent\n"
        "       • Server Members Intent\n"
        "  5. Click [bold]Reset Token[/] and copy it",
        title="[bold cyan]Discord Bot Setup[/]",
        border_style="cyan",
    ))
    if mode != "quickstart":
        c.print("  Docs: https://discord.com/developers/applications")

    bridges["discord"] = {
        "enabled": True,
        "options": {
            "token_env": "DISCORD_BOT_TOKEN", "allowed_users": [], "dm_enabled": True,
            "guild_ids": [], "prefix_required": True, "command_prefix": "!",
            "max_reply_length": 1900, "typing_indicator": True,
        },
    }

    if not os.environ.get("DISCORD_BOT_TOKEN"):
        c.print()
        entered = _prompt_secret("Paste your Discord bot token (or leave blank to set it later)")
        if entered:
            os.environ["DISCORD_BOT_TOKEN"] = entered
            try:
                set_env("DISCORD_BOT_TOKEN", entered)
                success("DISCORD_BOT_TOKEN saved to your shell profile and set for this session.")
            except Exception as e:
                warn(f"Could not persist DISCORD_BOT_TOKEN permanently ({e}) — set it manually before restarting.")
        else:
            warn("DISCORD_BOT_TOKEN not set yet — set it before starting.")
    else:
        success("DISCORD_BOT_TOKEN found.")


def _setup_matrix_bridge(bridges: dict, mode: Mode) -> None:
    import getpass
    import os
    from .helpers import set_env

    def _prompt_secret(prompt: str) -> str:
        try:
            v = getpass.getpass(prompt + ": ")
            return v.strip() if v else ""
        except (KeyboardInterrupt, EOFError):
            return ""
    c.print()
    c.print(Panel(
        "  1. Create a Matrix account at [bold]matrix.org[/] (or your own server)\n"
        "  2. Your MXID looks like: [bold]@yourname:matrix.org[/]",
        title="[bold cyan]Matrix Bot Setup[/]",
        border_style="cyan",
    ))

    homeserver = questionary.text("Homeserver URL", default="https://matrix.org", style=QSTYLE).ask() or "https://matrix.org"
    username   = questionary.text("Bot MXID (@bot:server.tld)", default="@yourbot:matrix.org", style=QSTYLE).ask() or "@yourbot:matrix.org"
    bridges["matrix"] = {
        "enabled": True,
        "options": {
            "homeserver": homeserver, "username": username, "password_env": "MATRIX_PASSWORD",
            "device_name": "TinyCTX", "store_path": "matrix_store", "allowed_users": [],
            "dm_enabled": True, "room_ids": [], "prefix_required": True,
            "command_prefix": "!", "max_reply_length": 16000, "sync_timeout_ms": 30000,
        },
    }

    if not os.environ.get("MATRIX_PASSWORD"):
        c.print()
        entered = _prompt_secret("Paste your Matrix bot password (or leave blank to set it later)")
        if entered:
            os.environ["MATRIX_PASSWORD"] = entered
            try:
                set_env("MATRIX_PASSWORD", entered)
                success("MATRIX_PASSWORD saved to your shell profile and set for this session.")
            except Exception as e:
                warn(f"Could not persist MATRIX_PASSWORD permanently ({e}) — set it manually before restarting.")
        else:
            warn("MATRIX_PASSWORD not set yet — set it before starting.")
    else:
        success("MATRIX_PASSWORD found.")


def step_max_tool_cycles(mode: Mode) -> int:
    if mode == "quickstart":
        return 25
    section("Agent Settings")
    c.print("Max tool cycles: how many tool calls the agent can make per turn before stopping.\n")
    return int(questionary.text("max_tool_cycles", default="25", style=QSTYLE).ask() or "25")


def step_skills(workspace: str, mode: Mode) -> None:
    zip_files = sorted(BUNDLED_DIR.glob("*.zip"))
    if not zip_files:
        return

    skills_dir = Path(workspace).expanduser() / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    existing = list(skills_dir.iterdir())
    if existing:
        if mode != "quickstart":
            c.print(f"  Skills directory already populated ({len(existing)} entries) — skipping.")
        return

    if mode == "quickstart":
        section("Installing Default Skills")
        for zp in zip_files:
            try:
                with zipfile.ZipFile(zp) as zf:
                    zf.extractall(skills_dir)
                success(f"Installed [bold]{zp.stem}[/] skill")
            except Exception as e:
                warn(f"Failed to extract {zp.name}: {e}")
        return

    section("Step 6 — Skills")
    if not questionary.confirm(
        f"Install {len(zip_files)} bundled skill(s) into workspace?", default=True, style=QSTYLE
    ).ask():
        warn("Skipping skill installation.")
        return

    for zp in zip_files:
        try:
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(skills_dir)
            success(f"Extracted [bold]{zp.name}[/]")
        except Exception as e:
            warn(f"Failed to extract {zp.name}: {e}")


def step_bootstrap_md(workspace: str) -> None:
    ws = Path(workspace).expanduser()
    for fname in BUNDLED_MD:
        src, dest = BUNDLED_DIR / fname, ws / fname
        if not src.exists() or dest.exists():
            continue
        try:
            shutil.copy2(src, dest)
            success(f"Installed bundled [bold]{fname}[/] → {dest}")
        except Exception as e:
            warn(f"Could not copy {fname}: {e}")


def step_launch(gateway: dict[str, Any]) -> None:
    import subprocess
    section("Launch")
    c.print("TinyCTX is configured. Would you like to start it now?\n")
    if not questionary.confirm("Start TinyCTX in a new terminal window?", default=True, style=QSTYLE).ask():
        c.print("  Run [bold]python -m main[/] when you're ready.")
        return

    import sys
    python = sys.executable
    cwd    = str(REPO_ROOT)
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", "cmd", "/k", python, "-m", "main"],
                cwd=cwd, close_fds=True,
            )
        elif sys.platform == "darwin":
            script = f'tell application "Terminal" to do script "cd {cwd} && {python} -m main"'
            subprocess.Popen(["osascript", "-e", script])
        else:
            for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
                try:
                    subprocess.Popen([term, "--", python, "-m", "main"], cwd=cwd)
                    break
                except FileNotFoundError:
                    continue
            else:
                warn("Could not find a terminal emulator — run [bold]python -m main[/] manually.")
                return
        host, port = gateway["host"], gateway["port"]
        success(f"Agent launched! Gateway will be at http://{host}:{port}")
    except Exception as e:
        warn(f"Could not open terminal ({e}) — run [bold]python -m main[/] manually.")


def step_summary(mode: Mode, gateway: dict[str, Any]) -> None:
    section("Done!")

    if mode == "quickstart":
        body = Markdown(f"""
**Config written to:** `{CONFIG_PATH}`

### Next steps

1. **Set your API key** (if you haven't already):
   - Windows: `set YOUR_PROVIDER_API_KEY=sk-...`
   - Mac/Linux: `export YOUR_PROVIDER_API_KEY=sk-...`

2. **Start TinyCTX:**
   ```
   python -m main
   ```

3. **Connect a client** (e.g. SillyTavern) to:
   - URL: `http://{gateway["host"]}:{gateway["port"]}`
   - API Key: `{gateway["api_key"]}`

That's it! If something doesn't work, re-run `python -m onboard`.
""")
        c.print(Panel(body, title="[bold green]TinyCTX is ready 🎉[/]", border_style="green"))

    elif mode == "standard":
        body = Markdown(f"""
**Config written to:** `{CONFIG_PATH}`

**Next steps:**

1. Set any missing environment variables (API keys, bot tokens).
2. Start the agent: `python -m main`
3. To reconfigure: re-run `python -m onboard`
""")
        c.print(Panel(body, title="[bold green]TinyCTX is ready[/]", border_style="green"))

    else:  # advanced
        body = Markdown(f"""
**Config written to:** `{CONFIG_PATH}`

**Next steps:**

1. Set any missing environment variables (API keys, bot tokens).
2. Start the agent: `python -m main`
3. To reconfigure: re-run `python -m onboard`
4. For advanced tuning, edit `config.yaml` directly  
   (see `example.config.yaml` for all knobs).
""")
        c.print(Panel(body, title="[bold green]TinyCTX is ready[/]", border_style="green"))


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="python -m onboard", description="TinyCTX Onboarding Wizard")
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
        c.print(Panel("[bold]Welcome![/] Let's get TinyCTX configured.", border_style="cyan"))

    mode: Mode = step_select_mode()

    # ── shared step sequence ──────────────────────────────────────────────────
    model_cfg       = step_llm(providers, beginner_providers, mode)
    embed_cfg       = step_embedding(providers, mode)          # None for quickstart
    workspace       = step_workspace(mode)
    gateway         = step_gateway(mode)
    bridges         = step_bridges(mode)
    max_tool_cycles = step_max_tool_cycles(mode)               # 25 for quickstart

    step_skills(workspace, mode)
    step_bootstrap_md(workspace)

    data = assemble_config(model_cfg, embed_cfg, workspace, gateway, bridges, max_tool_cycles, existing)
    write_config(data)
    success(f"Config written to [bold]{CONFIG_PATH}[/]")

    step_launch(gateway)
    step_summary(mode, gateway)


if __name__ == "__main__":
    main()

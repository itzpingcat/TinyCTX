"""
onboard/helpers.py — shared constants, pure utilities, UI primitives, and
config I/O for the TinyCTX onboarding wizard.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Literal

import questionary
import yaml
from questionary import Style
from rich.console import Console
from rich.rule import Rule

# ── paths & constants ─────────────────────────────────────────────────────────

REPO_ROOT               = Path(__file__).resolve().parent.parent
BUNDLED_DIR             = Path(__file__).parent / "bundled"
PROVIDERS_FILE          = Path(__file__).parent / "providers.json"
BEGINNER_PROVIDERS_FILE = Path(__file__).parent / "beginner-providers.json"
CONFIG_PATH             = REPO_ROOT / "config.yaml"

BANNER = r"""
 _______ _             _____ _______ _  __
|__   __(_)           / ____|__   __| |/ /
   | |   _ _ __  _   | |       | |  | ' /  __  __
   | |  | | '_ \| | | | |       | |  |  <   \ \/ /
   | |  | | | | | |_| | |____   | |  | . \   >  <
   |_|  |_|_| |_|\__, |\_____|  |_|  |_|\_\ /_/\_\
                  __/ |
                 |___/    Onboarding Wizard
"""

DEFAULT_WORKSPACE    = "~/.tinyctx"
DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 8080

BUNDLED_MD = ["SOUL.md", "AGENTS.md", "MEMORY.md"]

LOCAL_PROVIDERS = {"Ollama", "LMStudio", "vLLM", "llama-cpp", "Custom (local)"}

Mode = Literal["quickstart", "standard", "advanced"]

QSTYLE = Style([
    ("qmark",       "fg:#00cfff bold"),
    ("question",    "bold"),
    ("answer",      "fg:#00ff99 bold"),
    ("pointer",     "fg:#00cfff bold"),
    ("highlighted", "fg:#00cfff bold"),
    ("selected",    "fg:#00ff99"),
    ("separator",   "fg:#555555"),
    ("instruction", "fg:#888888"),
])

c = Console()


# ── navigation ───────────────────────────────────────────────────────────────

class GoBack(Exception):
    """Raised by any wizard step to return to the previous step."""


# ── UI primitives ─────────────────────────────────────────────────────────────

def section(title: str) -> None:
    c.print()
    c.print(Rule(f"[bold cyan]{title}[/]", style="cyan"))


def success(msg: str) -> None:
    c.print(f"[bold green]✓[/] {msg}")


def warn(msg: str) -> None:
    c.print(f"[bold yellow]![/] {msg}")


# ── data loaders ──────────────────────────────────────────────────────────────

def load_providers() -> dict[str, str]:
    """Load full providers list (name → base_url)."""
    with open(PROVIDERS_FILE) as f:
        return json.load(f)


def load_beginner_providers() -> dict[str, dict]:
    """Load enriched beginner providers list (name → {base_url, key_url, key_steps, suggested_models})."""
    with open(BEGINNER_PROVIDERS_FILE) as f:
        return json.load(f)


# ── config I/O ────────────────────────────────────────────────────────────────

def load_existing_config() -> dict | None:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                d = yaml.safe_load(f)
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return None


def write_config(data: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def assemble_config(
    model_cfg:       dict,
    embed_cfg:       dict | None,
    workspace:       str,
    gateway:         dict,
    bridges:         dict,
    max_tool_cycles: int,
    existing:        dict | None,
) -> dict:
    base = existing or {}

    models = base.get("models", {})
    models["primary"] = model_cfg
    if embed_cfg:
        models["embed"] = embed_cfg
    base["models"] = models

    base["llm"] = base.get("llm") or {
        "primary": "primary",
        "fallback": [],
        "fallback_on": {"any_error": False, "http_codes": [429, 500, 502, 503, 504]},
    }

    base.setdefault("context", 16384)
    base["workspace"] = {"path": workspace}
    base["gateway"]   = gateway

    existing_bridges = base.get("bridges", {})
    for name, bcfg in bridges.items():
        existing_bridges[name] = bcfg
    base["bridges"] = existing_bridges

    mem = base.get("memory_search", {})
    mem["auto_inject"] = True
    if embed_cfg:
        mem["embedding_model"] = "embed"
    base["memory_search"] = mem

    base.setdefault("logging", {"level": "INFO"})
    base["max_tool_cycles"] = max_tool_cycles

    return base


# ── network helpers ───────────────────────────────────────────────────────────

def api_key_env_for(provider_name: str) -> str:
    return provider_name.upper().replace(" ", "_").replace("-", "_") + "_API_KEY"


def is_valid_url(url: str) -> bool:
    """Return True if url has a recognised scheme and a non-empty netloc."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def fetch_models(base_url: str, api_key_env: str, timeout: float = 6.0) -> list[str]:
    """Query GET /v1/models. Returns sorted model ID list, or [] on failure."""
    if not is_valid_url(base_url):
        return []
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    url += "/models"

    api_key = os.environ.get(api_key_env, "") if api_key_env != "N/A" else "ignored"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return sorted(m["id"] for m in data.get("data", []) if "id" in m)
    except Exception:
        return []


def health_ping(host: str, port: int, timeout: float = 4.0) -> bool:
    url = f"http://{host}:{port}/v1/health"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ── model pickers ─────────────────────────────────────────────────────────────

def pick_model(base_url: str, api_key_env: str, label: str = "model") -> str:
    """
    Try to list models from the endpoint. If successful, show arrow-key select.
    Falls back to free-text entry if the endpoint is unreachable.
    Floats likely embedding models to the top when label contains 'embed'.
    """
    c.print(f"  Querying {base_url}/models …", end=" ")
    model_ids = fetch_models(base_url, api_key_env)

    if not model_ids:
        c.print("[yellow]could not reach endpoint[/]")
        answer = questionary.text(f"Enter {label} name manually", style=QSTYLE).ask()
        return answer or ""

    if "embed" in label.lower():
        hints   = ("embed", "nomic", "mxbai", "e5-", "bge-", "voyage", "gte-", "minilm", "all-")
        priority = [m for m in model_ids if any(h in m.lower() for h in hints)]
        rest     = [m for m in model_ids if m not in priority]
        ordered  = priority + rest
        c.print(f"[green]{len(model_ids)} models ({len(priority)} likely embedding)[/]")
    else:
        ordered = model_ids
        c.print(f"[green]{len(model_ids)} models found[/]")

    answer = questionary.select(f"Select {label}", choices=ordered, style=QSTYLE).ask()
    return answer or ""


def pick_model_beginner(provider_name: str, base_url: str, api_key_env: str, suggested: list[str]) -> str:
    """
    Quickstart model picker: tries live endpoint first.
    Falls back to the curated suggested_models list if unreachable.
    """
    c.print(f"  Querying available models …", end=" ")
    model_ids = fetch_models(base_url, api_key_env)

    if model_ids:
        c.print(f"[green]{len(model_ids)} models found[/]")
        answer = questionary.select("Select a model", choices=model_ids, style=QSTYLE).ask()
    else:
        c.print("[yellow]couldn't connect yet (API key may not be set) — showing recommended models[/]")
        choices = suggested + ["Enter manually…"]
        answer  = questionary.select("Pick a model to use", choices=choices, style=QSTYLE).ask()
        if answer == "Enter manually…":
            answer = questionary.text("Model name", style=QSTYLE).ask() or ""

    return answer or ""


# ── legacy Config / set_env (kept for other callers) ─────────────────────────

class Config:
    def __init__(self, file_path):
        self.file_path = file_path
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            with open(self.file_path, "r") as f:
                return yaml.safe_load(f) or {}
        return {}

    def set(self, key_path, value):
        keys    = key_path.strip("/").split("/")
        current = self.data
        for key in keys[:-1]:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value
        self._save()

    def _save(self):
        dir_name = os.path.dirname(self.file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(self.file_path, "w") as f:
            yaml.safe_dump(self.data, f, default_flow_style=False, sort_keys=False)


def set_env(key, value):
    """Sets an environment variable permanently based on the OS."""
    current_os = platform.system()
    if current_os == "Windows":
        subprocess.run(["setx", key, str(value)], check=True, capture_output=True)
    elif current_os in ["Linux", "Darwin"]:
        shell_profile = os.path.expanduser("~/.zshrc" if current_os == "Darwin" else "~/.bashrc")
        with open(shell_profile, "a") as f:
            f.write(f'\nexport {key}="{value}"\n')
    else:
        raise NotImplementedError(f"OS {current_os} not supported for permanent env.")

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
import socket, ipaddress
from urllib.parse import urlparse
import urllib.request
import urllib.error

# ── paths & constants ─────────────────────────────────────────────────────────

# onboard/ -> TinyCTX/ (package) -> repo root
REPO_ROOT               = Path(__file__).resolve().parent.parent.parent
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
DEFAULT_GATEWAY_PORT = 8085

BUNDLED_MD = ["SOUL.md", "AGENTS.md", "MEMORY.md"]

LOCAL_PROVIDERS = {"Ollama", "LMStudio", "vLLM", "llama-cpp", "Custom (local)"}

Mode = Literal["quickstart", "standard"]

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
    c.print(f"[bold green]OK[/] {msg}")


def warn(msg: str) -> None:
    c.print(f"[bold yellow]![/] {msg}")


# ── data loaders ──────────────────────────────────────────────────────────────

def load_providers() -> dict[str, str]:
    """Load full providers list (name -> base_url)."""
    with open(PROVIDERS_FILE) as f:
        return json.load(f)


def load_beginner_providers() -> dict[str, dict]:
    """Load beginner providers list (name -> {base_url, key_url, key_steps, suggested_models})."""
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
    models["primary"] = {k: v for k, v in model_cfg.items() if k != "context"}
    if embed_cfg:
        models["embed"] = embed_cfg
    base["models"] = models

    base["llm"] = base.get("llm") or {
        "primary": "primary",
        "fallback": [],
        "fallback_on": {"any_error": False, "http_codes": [429, 500, 502, 503, 504]},
    }

    base["context"] = model_cfg.get("context", 16384)
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


def fetch_models(base_url: str, api_key: str | None = None, timeout: float = 3.0) -> list[str] | None:
    """
    Queries GET /models. 
    Returns:
        - list[str]: Success (even if list is empty).
        - None: Authentication failed (401/403), key required.
        - []: Network error or other failure.
    """
    if not is_valid_url(base_url):
        return []

    # Normalize URL: ensures it ends in /v1/models or /models 
    # depending on your base_url structure
    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        # Try /v1/models first if /v1 is not already in the path
        if "/v1" not in url:
            url_v1 = url + "/v1/models"
        else:
            url_v1 = None
        url = url + "/models"
    else:
        url_v1 = None

    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "N/A":
        # Resolve env var if api_key looks like one
        actual_key = os.environ.get(api_key, api_key)
        headers["Authorization"] = f"Bearer {actual_key}"

    def _try_fetch(target_url: str) -> list[str] | None:
        req = urllib.request.Request(target_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                models = [m["id"] for m in data.get("data", []) if "id" in m]
                if not models and isinstance(data, list):
                    models = [m.get("id", m) if isinstance(m, dict) else m for m in data]
                return sorted(models)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return None  # auth required
            return []
        except Exception:
            return []

    # If we have a /v1/models candidate, try it first
    if url_v1:
        result = _try_fetch(url_v1)
        if result:  # non-empty list means success
            return result
        if result is None:  # 401/403 — propagate immediately
            return None
        # result == [] — fall through and try bare /models

    return _try_fetch(url)


def health_ping(host: str, port: int, timeout: float = 4.0) -> bool:
    url = f"http://{host}:{port}/v1/health"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

def is_local(url: str) -> bool:
    try:
        h = urlparse(url).hostname or url.split("/")[0]
        if not h:
            return False

        if h in ("localhost",) or h.endswith(".local"):
            return True

        def check(ip):
            return (
                ip.is_private or ip.is_loopback or
                ip.is_link_local or ip in TAILSCALE_NET
            )

        try:
            return check(ipaddress.ip_address(h))
        except ValueError:
            pass

        for a in socket.getaddrinfo(h, None):
            if check(ipaddress.ip_address(a[4][0])):
                return True

        return False
    except:
        return False

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

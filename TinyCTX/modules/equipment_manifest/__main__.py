"""
modules/equipment_manifest/__main__.py

Reads EM.md as a Jinja2 template, renders it with runtime variables, and
injects the result as a system prompt every turn.

Available template variables:
  system          — OS name: "Windows", "Darwin", "Linux", etc.
  date            — today's date, e.g. "2025-01-15"
  time            — current time, e.g. "14:32"
  workspace_path  — resolved absolute path to the workspace directory
  config_path     — resolved absolute path to config.yaml (best-effort)
  source_root     — cwd at launch time (where TinyCTX's own code lives);
                    equals workspace_path when launched from the workspace

Full Jinja2 syntax is supported: {% if %}/{% else %}/{% endif %}, {% for %},
filters, whitespace control ({%- -%}), macros, etc.

Path resolution for em_path config key:
  ""              — EM.md next to this __init__.py (module directory)
  "workspace:X"  — X resolved under workspace root
  relative path   — resolved under workspace root
  absolute path   — used as-is

If EM.md is missing or empty after rendering, the module is a silent no-op.

Convention: register(agent) — no imports from gateway or bridges.
"""
from __future__ import annotations

import logging
import platform
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, TemplateSyntaxError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Variable builder
# ---------------------------------------------------------------------------

def _build_variables(agent) -> dict[str, str]:
    now       = datetime.now()
    workspace = Path(agent.config.workspace.path).expanduser().resolve()
    source_root = Path.cwd().resolve()

    config_path = ""
    raw = getattr(agent.config, "config_path", None)
    if raw:
        try:
            config_path = str(Path(raw).expanduser().resolve())
        except Exception:
            config_path = str(raw)

    return {
        "system":         platform.system(),
        "date":           now.strftime("%Y-%m-%d"),
        "time":           now.strftime("%H:%M"),
        "workspace_path": str(workspace),
        "config_path":    config_path,
        "source_root":    str(source_root),
    }


# ---------------------------------------------------------------------------
# EM.md path resolution
# ---------------------------------------------------------------------------

def _resolve_em_path(em_path_cfg: str, module_dir: Path, workspace: Path) -> Path:
    if not em_path_cfg:
        return module_dir / "EM.md"
    if em_path_cfg.startswith("workspace:"):
        return (workspace / em_path_cfg[len("workspace:"):]).resolve()
    p = Path(em_path_cfg)
    return p if p.is_absolute() else (workspace / p).resolve()


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def register(agent) -> None:
    # Load config
    try:
        from TinyCTX.modules.equipment_manifest import EXTENSION_META
        cfg: dict = dict(EXTENSION_META.get("default_config", {}))
    except ImportError:
        cfg = {}

    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        for k, v in agent.config.extra.get("equipment_manifest", {}).items():
            cfg[k] = v

    if not cfg.get("enabled", True):
        logger.info("[equipment_manifest] disabled via config")
        return

    workspace  = Path(agent.config.workspace.path).expanduser().resolve()
    module_dir = Path(__file__).parent.resolve()
    em_path    = _resolve_em_path(str(cfg.get("em_path", "")), module_dir, workspace)
    priority   = int(cfg.get("prompt_priority", 5))

    if not em_path.exists():
        logger.debug("[equipment_manifest] EM.md not found at %s — module inactive", em_path)
        return

    # One Jinja2 Environment per registered prompt; FileSystemLoader lets
    # templates use {% include %} relative to their own directory.
    jinja_env = Environment(
        loader=FileSystemLoader(str(em_path.parent)),
        keep_trailing_newline=True,
        autoescape=False,
    )

    def _em_prompt(_ctx) -> str | None:
        try:
            template = jinja_env.get_template(em_path.name)
        except TemplateNotFound:
            return None
        except TemplateSyntaxError as exc:
            logger.warning("[equipment_manifest] syntax error in %s: %s", em_path, exc)
            return None

        variables = _build_variables(agent)
        try:
            rendered = template.render(**variables).strip()
        except Exception as exc:
            logger.warning("[equipment_manifest] render error in %s: %s", em_path, exc)
            return None

        return rendered or None

    agent.context.register_prompt(
        "equipment_manifest",
        _em_prompt,
        role="system",
        priority=priority,
    )

    logger.info("[equipment_manifest] registered — em_path=%s, priority=%d", em_path, priority)

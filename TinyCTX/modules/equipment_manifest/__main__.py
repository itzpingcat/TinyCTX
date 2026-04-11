"""
modules/equipment_manifest/__main__.py

Reads EM.md as a Jinja2 template, renders it with runtime variables, and
injects the result as a system prompt every turn.

Prompt-cache split
------------------
To avoid busting the LLM's prompt cache on every turn, the manifest is
split into two registered prompts:

  equipment_manifest        — role=system, static variables only.
                              All Jinja2 variables EXCEPT time are available.
                              This block is cache-stable between turns.

  equipment_manifest_footer — role=user, volatile variables only.
                              Rendered from EM_FOOTER.md (if present) or from
                              a {% block footer %}…{% endblock %} mechanism.
                              Defaults to a tiny "<clock>HH:MM</clock>" blurb
                              if no footer template file exists.
                              Sits outside the cached region entirely.

Footer template resolution
--------------------------
  1. EM_FOOTER.md next to EM.md         — used if present, rendered with ALL vars
  2. No file found                       — a built-in one-liner is emitted

Available template variables (both templates):
  system          — OS name: "Windows", "Darwin", "Linux", etc.
  date            — today's date, e.g. "2025-01-15"
  time            — current time, e.g. "14:32"
  workspace_path  — resolved absolute path to the workspace directory
  config_path     — resolved absolute path to config.yaml (best-effort)
  source_root     — cwd at launch time (where TinyCTX's own code lives);
                    equals workspace_path when launched from the workspace
  is_group_chat   — True when the current lane has a GroupPolicy attached
                    (i.e. a multi-user group channel/room). False for DMs
                    and synthetic turns. Read from ctx.state["group_policy"].
  is_dm           — Opposite of is_group_chat. True for 1:1 DM lanes.
  platform        — Bridge platform string: "discord", "matrix", "cli",
                    "api", "cron", or "" for synthetic/unknown turns.
                    Read from ctx.state["platform"].
  trusted         — True when the current user is in the trusted_users list
                    in equipment_manifest config (format: "platform:user_id").
                    Always False for group chats — trust is DM-only.
                    Read from ctx.state["author_id"] + ctx.state["platform"].

NOTE: For best cache efficiency, avoid using {{ time }} or {{ date }} inside
EM.md. Put time-sensitive content in EM_FOOTER.md instead.
is_group_chat, is_dm, and platform are stable for the lifetime of a lane,
so they are safe to use in EM.md without busting the cache.

Full Jinja2 syntax is supported in both templates.

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
import platform as platform_module
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, TemplateSyntaxError

logger = logging.getLogger(__name__)

_FOOTER_FILENAME = "EM_FOOTER.md"

# Built-in footer used when no EM_FOOTER.md exists.
# Jinja2 template string — has access to all variables.
_DEFAULT_FOOTER_TEMPLATE = "<clock>{{ time }}</clock>"


# ---------------------------------------------------------------------------
# Variable builder
# ---------------------------------------------------------------------------

def _build_variables(agent, ctx=None, trusted_users: frozenset = frozenset()) -> dict:
    now         = datetime.now()
    workspace   = Path(agent.config.workspace.path).expanduser().resolve()
    source_root = Path.cwd().resolve()

    config_path = ""
    raw = getattr(agent.config, "config_path", None)
    if raw:
        try:
            config_path = str(Path(raw).expanduser().resolve())
        except Exception:
            config_path = str(raw)

    is_group_chat = bool(
        ctx is not None and ctx.state.get("group_policy") is not None
    )
    platform = (ctx.state.get("platform") or "") if ctx is not None else ""
    author_id = (ctx.state.get("author_id") or "") if ctx is not None else ""
    # Trust is only meaningful in DMs — never grant it in group chats.
    trusted = (
        not is_group_chat
        and bool(platform)
        and bool(author_id)
        and f"{platform}:{author_id}" in trusted_users
    )

    return {
        "system":         platform_module.system(),
        "date":           now.strftime("%Y-%m-%d"),
        "time":           now.strftime("%H:%M"),
        "workspace_path": str(workspace),
        "config_path":    config_path,
        "source_root":    str(source_root),
        "is_group_chat":  is_group_chat,
        "is_dm":          not is_group_chat,
        "platform":       platform,
        "trusted":        trusted,
        "server_name":    (ctx.state.get("server_name") or "") if ctx is not None else "",
        "channel_name":   (ctx.state.get("channel_name") or "") if ctx is not None else "",
    }


def _build_static_variables(agent, ctx=None, trusted_users: frozenset = frozenset()) -> dict:
    """Like _build_variables but omits `time` so the result is cache-stable."""
    workspace   = Path(agent.config.workspace.path).expanduser().resolve()
    source_root = Path.cwd().resolve()

    config_path = ""
    raw = getattr(agent.config, "config_path", None)
    if raw:
        try:
            config_path = str(Path(raw).expanduser().resolve())
        except Exception:
            config_path = str(raw)

    # is_group_chat is stable for the lifetime of a lane — safe in the cached block.
    is_group_chat = bool(
        ctx is not None and ctx.state.get("group_policy") is not None
    )
    platform = (ctx.state.get("platform") or "") if ctx is not None else ""
    author_id = (ctx.state.get("author_id") or "") if ctx is not None else ""
    trusted = (
        not is_group_chat
        and bool(platform)
        and bool(author_id)
        and f"{platform}:{author_id}" in trusted_users
    )

    return {
        "system":         platform_module.system(),
        "date":           datetime.now().strftime("%Y-%m-%d"),  # changes at midnight only
        "workspace_path": str(workspace),
        "config_path":    config_path,
        "source_root":    str(source_root),
        "is_group_chat":  is_group_chat,
        "is_dm":          not is_group_chat,
        "platform":       platform,
        "trusted":        trusted,
        "server_name":    (ctx.state.get("server_name") or "") if ctx is not None else "",
        "channel_name":   (ctx.state.get("channel_name") or "") if ctx is not None else "",
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
    trusted_users = frozenset(
        str(u) for u in cfg.get("trusted_users", []) if u
    )

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

    # ------------------------------------------------------------------
    # Static top — system role, cache-stable
    # ------------------------------------------------------------------

    def _em_prompt_top(ctx) -> str | None:
        try:
            template = jinja_env.get_template(em_path.name)
        except TemplateNotFound:
            return None
        except TemplateSyntaxError as exc:
            logger.warning("[equipment_manifest] syntax error in %s: %s", em_path, exc)
            return None

        variables = _build_static_variables(agent, ctx, trusted_users)
        try:
            rendered = template.render(**variables).strip()
        except Exception as exc:
            logger.warning("[equipment_manifest] render error in %s: %s", em_path, exc)
            return None

        return rendered or None

    agent.context.register_prompt(
        "equipment_manifest",
        _em_prompt_top,
        role="system",
        priority=priority,
    )

    # ------------------------------------------------------------------
    # Volatile footer — user role, not cached
    # ------------------------------------------------------------------

    footer_path = em_path.parent / _FOOTER_FILENAME
    has_footer_file = footer_path.exists()

    if has_footer_file:
        logger.info("[equipment_manifest] footer template: %s", footer_path)
    else:
        logger.debug(
            "[equipment_manifest] no %s found — using built-in footer", _FOOTER_FILENAME
        )

    # Compile the built-in footer once (it's a simple string template, not
    # loaded via FileSystemLoader, so we use Environment.from_string).
    _builtin_footer_tmpl = jinja_env.from_string(_DEFAULT_FOOTER_TEMPLATE)

    def _em_prompt_footer(ctx) -> str | None:
        variables = _build_variables(agent, ctx, trusted_users)

        if has_footer_file:
            try:
                tmpl = jinja_env.get_template(_FOOTER_FILENAME)
            except (TemplateNotFound, TemplateSyntaxError) as exc:
                logger.warning(
                    "[equipment_manifest] footer template error in %s: %s", footer_path, exc
                )
                return None
            try:
                rendered = tmpl.render(**variables).strip()
            except Exception as exc:
                logger.warning(
                    "[equipment_manifest] footer render error in %s: %s", footer_path, exc
                )
                return None
            return rendered or None

        # Built-in one-liner
        try:
            return _builtin_footer_tmpl.render(**variables).strip() or None
        except Exception as exc:
            logger.warning("[equipment_manifest] built-in footer render error: %s", exc)
            return None

    # Footer priority is one higher than top so it sorts after, but since it's
    # role=user it will be placed in the messages list after all system blocks
    # regardless of priority ordering within the system block.
    agent.context.register_prompt(
        "equipment_manifest_footer",
        _em_prompt_footer,
        role="user",
        priority=priority + 1,
    )

    logger.info(
        "[equipment_manifest] registered — em_path=%s, priority=%d, footer=%s",
        em_path, priority, "file" if has_footer_file else "built-in",
    )

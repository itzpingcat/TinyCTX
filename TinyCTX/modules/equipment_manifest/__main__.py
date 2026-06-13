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
  is_group_chat   — True when server_name is set in session state (i.e. a
                    group channel or thread). False for DMs and synthetic turns.
  is_dm           — Opposite of is_group_chat. True for 1:1 DM lanes.
  platform        — Bridge platform string: "discord", "matrix", "cli",
                    "api", "cron", or "" for synthetic/unknown turns.
  trusted         — True when the current user has permission_level >= trusted_threshold
                    in the UserStore. True in both DMs and group chats.
                    Read from ctx.state["author_id"] + ctx.state["platform"].
  time_since_last_message — human-readable elapsed time since the previous user
                    message (e.g. "42s", "5m", "1h 3m"). Empty string on the
                    first message in a session or if unavailable.

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

Convention: register_agent(agent) — no imports from gateway or bridges.
"""
from __future__ import annotations

import logging
import platform as platform_module
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, TemplateSyntaxError

from TinyCTX.context import HOOK_PRE_ASSEMBLE_ASYNC

logger = logging.getLogger(__name__)

# Module-level reference set by register_runtime.
_users = None

_FOOTER_FILENAME = "EM_FOOTER.md"

# Built-in footer used when no EM_FOOTER.md exists.
# Jinja2 template string — has access to all variables.
_DEFAULT_FOOTER_TEMPLATE = "<clock>{{ time }}</clock>"


# ---------------------------------------------------------------------------
# Variable builder
# ---------------------------------------------------------------------------

def _build_variables(agent, ctx=None, trusted_threshold: int = 90, last_message_at: float | None = None) -> dict:
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

    session = ctx.state.get("session", {}) if ctx is not None else {}
    is_group_chat = bool(session.get("server_name"))
    platform = session.get("platform") or ""
    author_id = session.get("author_id") or ""
    # Trust check — applies in both DMs and group chats.
    trusted = False
    if _users is not None and platform and author_id:
        try:
            from TinyCTX.contracts import Platform
            user = _users.get_by_platform(Platform(platform), author_id)
            trusted = user is not None and user.permission_level >= trusted_threshold
        except Exception as exc:
            logger.debug("[equipment_manifest] trusted lookup failed: %s", exc)

    # Format time since last message
    time_since_last_message = ""
    if last_message_at is not None:
        elapsed = int(datetime.now().timestamp() - last_message_at)
        if elapsed < 60:
            time_since_last_message = f"{elapsed}s"
        elif elapsed < 3600:
            time_since_last_message = f"{elapsed // 60}m"
        else:
            time_since_last_message = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"

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
        "server_name":    session.get("server_name") or "",
        "channel_name":   session.get("channel_name") or "",
        "time_since_last_message": time_since_last_message,
    }


def _build_static_variables(agent, ctx=None, trusted_threshold: int = 90) -> dict:
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
    session = ctx.state.get("session", {}) if ctx is not None else {}
    is_group_chat = bool(session.get("server_name"))
    platform = session.get("platform") or ""
    author_id = session.get("author_id") or ""
    trusted = False
    if _users is not None and platform and author_id:
        try:
            from TinyCTX.contracts import Platform
            user = _users.get_by_platform(Platform(platform), author_id)
            trusted = user is not None and user.permission_level >= trusted_threshold
        except Exception as exc:
            logger.debug("[equipment_manifest] trusted lookup failed: %s", exc)

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
        "server_name":    session.get("server_name") or "",
        "channel_name":   session.get("channel_name") or "",
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

def register_runtime(runtime) -> None:
    global _users
    _users = runtime.users
    logger.info("[equipment_manifest] registered — UserStore at %s", id(_users))


def register_agent(agent) -> None:
    # Normalise: accept an AgentCycle
    # Wrap Runtime in a minimal shim so the rest of register() is unchanged.

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
    trusted_threshold = int(cfg.get("trusted_threshold", 90))

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

    # Stash for the async hook to write into, footer provider reads from.
    _last_message_ts: list[float | None] = [None]

    async def _fetch_last_message_time(ctx) -> None:
        """Pre-assemble hook: find created_at of the most recent prior user node."""
        try:
            ancestors = agent.db.get_ancestors(ctx.tail_node_id)
            # Walk tip→root looking for a user node that isn't the current tip.
            for node in reversed(ancestors[:-1]):
                if node.role == "user":
                    _last_message_ts[0] = node.created_at
                    return
        except Exception as exc:
            logger.debug("[equipment_manifest] last_message_at lookup failed: %s", exc)
        _last_message_ts[0] = None

    agent.context.register_hook(HOOK_PRE_ASSEMBLE_ASYNC, _fetch_last_message_time)

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

        variables = _build_static_variables(agent, ctx, trusted_threshold)
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
        variables = _build_variables(agent, ctx, trusted_threshold, _last_message_ts[0])

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

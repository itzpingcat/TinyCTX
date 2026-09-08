"""
modules/equipment_manifest/__init__.py

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
                              a built-in one-liner. Sits outside the cached
                              region entirely.

Footer template resolution
--------------------------
  1. EM_FOOTER.md next to EM.md         — used if present, rendered with ALL vars
  2. No file found                       — a built-in one-liner is emitted

Available template variables (both templates):
  system          — OS name: "Windows", "Darwin", "Linux", etc.
  date            — today's date in UTC, e.g. "2025-01-15"
  time            — current time in UTC, e.g. "14:32 UTC" (explicitly
                    labelled — this is always UTC regardless of the host
                    machine's local timezone, so callers computing a future
                    timestamp from it, e.g. modules/cron's one-shot
                    reminders, never need to guess or convert)
  workspace_path  — resolved absolute path to the workspace directory
  config_path     — resolved absolute path to config.yaml (best-effort)
  source_root     — cwd at launch time (where TinyCTX's own code lives);
                    equals workspace_path when launched from the workspace
  is_group_chat   — True when server_name is set in session state (i.e. a
                    group channel or thread). False for DMs and synthetic turns.
  is_dm           — Opposite of is_group_chat. True for 1:1 DM lanes.
  platform        — Bridge platform string: "discord", "matrix", "cli",
                    "api", "cron", or "" for synthetic/unknown turns.
  trusted         — True when the current user holds Permission.EQUIPMENT_TRUSTED
                    (docs/PERMISSIONS-PLAN.md §10.3 — a disclosure flag, not
                    an authorisation bool). True in both DMs and group chats.
                    Read by looking up ctx.state["session"]["author_id"] (the
                    TinyCTX username) via UserStore.get_user() — NOT
                    get_by_platform(), which wants a platform-native user_id
                    and would silently miss.
  time_since_last_message — human-readable elapsed time since the previous user
                    message (e.g. "42s", "5m", "1h 3m"). Empty string on the
                    first message in a session or if unavailable.

NOTE: For best cache efficiency, avoid using {{ time }} or {{ date }} inside
EM.md. Put time-sensitive content in EM_FOOTER.md instead.
is_group_chat, is_dm, and platform are stable for the lifetime of a lane,
so they are safe to use in EM.md without busting the cache.

Full Jinja2 syntax is supported in both templates.

Path resolution for em_path config key:
  ""              — EM.md next to this file (module directory)
  "workspace:X"  — X resolved under workspace root
  relative path   — resolved under workspace root
  absolute path   — used as-is

If EM.md is missing or empty after rendering, the module is a silent no-op.
"""
from __future__ import annotations

import logging
import platform as platform_module
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, TemplateSyntaxError

from TinyCTX.decorators import hook, prompt
from TinyCTX.hooks import HookType
from TinyCTX.module import Module
from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)

_FOOTER_FILENAME = "EM_FOOTER.md"

# Built-in footer used when no EM_FOOTER.md exists.
# Jinja2 template string — has access to all variables.
_DEFAULT_FOOTER_TEMPLATE = "<clock>{{ time }}</clock>"


class EquipmentManifest(Module):
    """Injects a rendered Equipment Manifest (EM.md) as a system prompt."""

    settings = {
        "em_path": {
            "default": "",
            "type": "str",
            "description": "Path to EM.md — empty for the file next to this module, "
                            "'workspace:X' or a relative path for workspace-relative, "
                            "or an absolute path.",
        },
        "enabled": {
            "default": True,
            "type": "bool",
            "description": "Set false to disable this module without removing it from config.",
        },
    }

    # @prompt's priority is fixed at class-definition time (decorators run
    # before any instance/config exists — see decorators.py), so unlike the
    # old register_agent()'s cfg.get("prompt_priority", 5) this is no longer
    # configurable via settings. Matches MODULES-PLAN-P1.md's own worked
    # example, which hardcodes this module's @prompt priority the same way.
    _PROMPT_PRIORITY = 5

    def __init__(self) -> None:
        self._users = None
        self._active = False

    # -----------------------------------------------------------------
    # STARTUP — replaces register_runtime() + register_agent()'s one-time
    # (per-cycle, today) setup: resolve EM.md, build the Jinja2 Environment,
    # compile the built-in footer. None of this depends on per-turn data.
    # -----------------------------------------------------------------

    @hook(HookType.STARTUP)
    def load(self, runtime) -> None:
        self._users = runtime.users
        self._permissions_cfg = runtime.config.permissions

        if not self.config["enabled"]:
            logger.info("[equipment_manifest] disabled via config")
            return

        workspace = Path(runtime.config.workspace.path).expanduser().resolve()
        module_dir = Path(__file__).parent.resolve()
        em_path = _resolve_em_path(str(self.config["em_path"]), module_dir, workspace)

        if not em_path.exists():
            logger.debug("[equipment_manifest] EM.md not found at %s — module inactive", em_path)
            return

        self._workspace = workspace
        self._source_root = Path.cwd().resolve()
        raw_config_path = getattr(runtime.config, "config_path", None)
        self._config_path = ""
        if raw_config_path:
            try:
                self._config_path = str(Path(raw_config_path).expanduser().resolve())
            except Exception:
                self._config_path = str(raw_config_path)

        self._em_path = em_path
        # One Jinja2 Environment; FileSystemLoader lets templates use
        # {% include %} relative to their own directory.
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(em_path.parent)),
            keep_trailing_newline=True,
            autoescape=False,
        )
        footer_path = em_path.parent / _FOOTER_FILENAME
        self._has_footer_file = footer_path.exists()
        self._builtin_footer_tmpl = self._jinja_env.from_string(_DEFAULT_FOOTER_TEMPLATE)
        self._active = True

        logger.info(
            "[equipment_manifest] registered — em_path=%s, footer=%s",
            em_path, "file" if self._has_footer_file else "built-in",
        )

    # -----------------------------------------------------------------
    # PRE_ASSEMBLE — precompute the footer's one volatile, DB-backed value
    # once per assemble pass, synchronously, for the footer @prompt to read
    # back out of scratch. See MODULES-PLAN-P1.md's @prompt worked
    # comparison — this is the "correct" (non-racy) precompute-ahead shape.
    # -----------------------------------------------------------------

    @hook(HookType.PRE_ASSEMBLE)
    def fetch_last_message_time(self, ctx, scratch) -> None:
        scratch.last_message_ts = None
        if not self._active:
            return
        try:
            ancestors = ctx.db.get_ancestors(ctx.tail_node_id)
            for node in reversed(ancestors[:-1]):
                if node.role == "user":
                    scratch.last_message_ts = node.created_at
                    return
        except Exception as exc:
            logger.debug("[equipment_manifest] last_message_at lookup failed: %s", exc)

    # -----------------------------------------------------------------
    # Static top — system role, cache-stable
    # -----------------------------------------------------------------

    @prompt(role="system", priority=_PROMPT_PRIORITY, name="equipment_manifest")
    def top(self, ctx) -> str | None:
        if not self._active:
            return None
        try:
            template = self._jinja_env.get_template(self._em_path.name)
        except TemplateNotFound:
            return None
        except TemplateSyntaxError as exc:
            logger.warning("[equipment_manifest] syntax error in %s: %s", self._em_path, exc)
            return None

        variables = self._build_static_variables(ctx)
        try:
            rendered = template.render(**variables).strip()
        except Exception as exc:
            logger.warning("[equipment_manifest] render error in %s: %s", self._em_path, exc)
            return None
        return rendered or None

    # -----------------------------------------------------------------
    # Volatile footer — user role, not cached
    # -----------------------------------------------------------------

    @prompt(role="user", priority=_PROMPT_PRIORITY + 1, name="equipment_manifest_footer")
    def footer(self, ctx, scratch) -> str | None:
        if not self._active:
            return None
        variables = self._build_variables(ctx, scratch.last_message_ts)

        if self._has_footer_file:
            try:
                tmpl = self._jinja_env.get_template(_FOOTER_FILENAME)
            except (TemplateNotFound, TemplateSyntaxError) as exc:
                logger.warning("[equipment_manifest] footer template error: %s", exc)
                return None
            try:
                return tmpl.render(**variables).strip() or None
            except Exception as exc:
                logger.warning("[equipment_manifest] footer render error: %s", exc)
                return None

        try:
            return self._builtin_footer_tmpl.render(**variables).strip() or None
        except Exception as exc:
            logger.warning("[equipment_manifest] built-in footer render error: %s", exc)
            return None

    # -----------------------------------------------------------------
    # Variable builders
    # -----------------------------------------------------------------

    def _trusted(self, session: dict) -> bool:
        # session["author_id"] is the TinyCTX *username* (see
        # Runtime._compute_state_delta, which sets it from msg.author.username
        # — msg.author is already a resolved TinyCTX.users.User), NOT the
        # platform-native user_id. Look the user up by username via
        # get_user(), not get_by_platform() (that wants a platform snowflake
        # and would silently miss, leaving `trusted` stuck at False even for
        # a user who holds EQUIPMENT_TRUSTED).
        author_id = session.get("author_id") or ""
        if self._users is None or not author_id:
            return False
        try:
            user = self._users.get_user(author_id)
            return user is not None and user.has_permission(Permission.EQUIPMENT_TRUSTED, self._permissions_cfg)
        except Exception as exc:
            logger.debug("[equipment_manifest] trusted lookup failed: %s", exc)
            return False

    def _build_static_variables(self, ctx) -> dict:
        """Like _build_variables but omits `time` so the result is cache-stable."""
        session = ctx.state.get("session", {})
        is_group_chat = bool(session.get("server_name"))
        return {
            "system":         platform_module.system(),
            "date":           datetime.now(timezone.utc).strftime("%Y-%m-%d"),  # UTC; changes at midnight UTC only
            "workspace_path": str(self._workspace),
            "config_path":    self._config_path,
            "source_root":    str(self._source_root),
            "is_group_chat":  is_group_chat,
            "is_dm":          not is_group_chat,
            "platform":       session.get("platform") or "",
            "trusted":        self._trusted(session),
            "server_name":    session.get("server_name") or "",
            "channel_name":   session.get("channel_name") or "",
        }

    def _build_variables(self, ctx, last_message_at: float | None) -> dict:
        now = datetime.now(timezone.utc)
        variables = self._build_static_variables(ctx)
        variables["time"] = now.strftime("%H:%M UTC")

        time_since_last_message = ""
        if last_message_at is not None:
            elapsed = int(now.timestamp() - last_message_at)
            if elapsed < 60:
                time_since_last_message = f"{elapsed}s"
            elif elapsed < 3600:
                time_since_last_message = f"{elapsed // 60}m"
            else:
                time_since_last_message = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
        variables["time_since_last_message"] = time_since_last_message
        return variables


def _resolve_em_path(em_path_cfg: str, module_dir: Path, workspace: Path) -> Path:
    if not em_path_cfg:
        return module_dir / "EM.md"
    if em_path_cfg.startswith("workspace:"):
        return (workspace / em_path_cfg[len("workspace:"):]).resolve()
    p = Path(em_path_cfg)
    return p if p.is_absolute() else (workspace / p).resolve()

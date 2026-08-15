"""
config.py — Configuration loader.
Imports only from stdlib, PyYAML, and TinyCTX.permissions (pure enum data,
no I/O — same foundational layer as contracts.py). Never imports from
contracts or gateway.
"""
from __future__ import annotations
import logging, os
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from TinyCTX.permissions import Permission

logger = logging.getLogger(__name__)

# Built-in template defaults — used for any template name a config.yaml
# doesn't override under permissions.templates. A config that specifies no
# permissions.templates key at all gets exactly these four, matching
# docs/PERMISSIONS-PLAN.md §2's worked example. Named templates are what
# "elevate" becomes now that there's no int ladder to climb — see that
# section for why full explicit lists (no inheritance) are deliberate.
_BUILTIN_TEMPLATES: dict[str, frozenset[Permission]] = {
    "guest": frozenset(),
    "member": frozenset({
        Permission.FILE_READ, Permission.NETWORK_READ, Permission.MEMORY_READ,
    }),
    "trusted": frozenset({
        Permission.FILE_READ, Permission.FILE_WRITE,
        Permission.NETWORK_READ, Permission.NETWORK_WRITE,
        Permission.MEMORY_READ, Permission.MEMORY_WRITE,
        Permission.MANAGE_CTX, Permission.MODEL_SWAP,
        Permission.CRON_CREATE, Permission.DM_ACCESS,
        Permission.USER_READ, Permission.IMAGE_GEN,
        # NOTE: no UNTRUSTED_EXEC — see docs/PERMISSIONS-PLAN.md §5.1 for
        # what this costs (python3/node/etc. fall through to UNTRUSTED_EXEC
        # and a `trusted` user cannot run them until promoted).
    }),
    "operator": frozenset(Permission),  # every bool true
}


@dataclass
class ModelConfig:
    """
    One named model entry under models:.

    kind controls how the model is used:
      "chat"      — standard /v1/chat/completions  (default)
      "embedding" — /v1/embeddings, used by modules like memory/rag.
                    max_tokens and temperature are ignored for embeddings.
    """
    model:       str
    base_url:    str
    kind:        str   = "chat"       # "chat" | "embedding"
    api_key_env: str   = "ANTHROPIC_API_KEY"
    _resolved_api_key: str | None = field(default=None, init=False, repr=False, compare=False)
    max_tokens:       int        = 2048
    temperature:      float      = 0.7
    budget_tokens:    int | None = None   # Anthropic extended thinking: budget_tokens > 0
    reasoning_effort: str | None = None   # OpenAI-compat: "low" | "medium" | "high"
    cache_prompts:      bool        = False  # Anthropic prompt caching on last system message
    vision:             bool        = False  # Back-compat alias for multimodal chat models
    tokens_per_image:   int | None  = None   # Flat token cost per image_url block (None = vision disabled)
    context:            int         = 16384  # Token budget for conversation history when this model is primary (Context.token_limit)
    timeout:            int         = 60     # Seconds allowed between chunks/bytes with no data before aborting (aiohttp sock_read)
    query_template:     str         = "{text}"  # embedding models only: wraps search queries before embedding
    document_template:  str         = "{text}"  # embedding models only: wraps indexed content before embedding

    def __post_init__(self) -> None:
        # Back-compat: older configs/tests use `vision: true` without specifying
        # an explicit token charge for image_url blocks.
        if self.tokens_per_image is None and self.vision:
            self.tokens_per_image = 280
        elif self.tokens_per_image is not None:
            self.vision = True

    @property
    def supports_vision(self) -> bool:
        """True when the model accepts image_url content blocks."""
        return bool(self.vision or self.tokens_per_image is not None)

    @property
    def api_key(self) -> str:
        if not self.api_key_env or self.api_key_env.upper() == "N/A":
            return ""
        if self._resolved_api_key is not None:
            return self._resolved_api_key
        key = os.environ.pop(self.api_key_env, "").strip()
        if not key:
            raise EnvironmentError(
                f"API key not set. Export {self.api_key_env} before starting."
            )
        object.__setattr__(self, "_resolved_api_key", key)
        return key

    @property
    def is_embedding(self) -> bool:
        return self.kind.lower() == "embedding"


@dataclass
class PermissionsConfig:
    """
    Controls how the permission system interacts with the LLM's tool list,
    and defines the named permission templates users are assigned to.

    Configured via the top-level 'permissions:' key in config.yaml:

        permissions:
          minimal_tokens: true
          default_template: guest
          templates:
            guest: {}
            member:
              file_read: true
              network_read: true
              memory_read: true
            trusted:
              file_read: true
              file_write: true
              ...
            operator:
              file_read: true
              ...

    minimal_tokens: true  (default)
        Only tools the caller has permission to execute are sent to the LLM.
        The LLM never sees higher-privilege tools — saves tokens and prevents
        the model from being confused by tools it cannot use.

    minimal_tokens: false
        All enabled tools are sent to the LLM regardless of permission level.
        The LLM can see and attempt to call any tool. The execution-time guard
        in execute_tool_call() still enforces permissions — the call will return
        a PERMISSION DENIED error rather than execute. Useful when you want the
        agent to be aware of what exists and explain why it can't do something.

    default_template
        The template new users get when resolve_user() creates them, and the
        fallback used when a stored user's permission_template is empty or
        names a template that no longer exists in this config (with a
        logged warning — see docs/PERMISSIONS-PLAN.md §2).

    templates
        Named, FULLY EXPLICIT permission sets — no inheritance between them.
        Any permission bool not listed for a template is false. Each entry
        not overridden here falls back to _BUILTIN_TEMPLATES (guest/member/
        trusted/operator), so a config that says nothing under `templates:`
        still gets sane behavior; a config that overrides one template name
        leaves the other built-ins as-is unless it also names them.
    """
    minimal_tokens:    bool = False
    default_template:  str  = "guest"
    templates:         dict[str, frozenset[Permission]] = field(
        default_factory=lambda: dict(_BUILTIN_TEMPLATES)
    )

    def resolve_template(self, name: str) -> frozenset[Permission]:
        """
        Resolve a template name to its permission set. Falls back to
        default_template with a logged warning if `name` is empty or not a
        known template — deleting a template from config, or a stale
        per-user permission_template value, must not brick that user.
        """
        if name and name in self.templates:
            return self.templates[name]
        if name:
            logger.warning(
                "permissions: unknown template %r, falling back to default_template %r",
                name, self.default_template,
            )
        return self.templates.get(self.default_template, frozenset())


@dataclass
class ToolOverrideConfig:
    """
    Per-tool override of registration-time defaults (currently: always_on).

    Configured via 'tools.overrides:' in config.yaml:

        tools:
          overrides:
            present:
              always_on: true
            memory_search:
              always_on: false

    Fields left unset (null/omitted) leave that aspect of the tool untouched —
    only the fields you specify are overridden. Unknown tool names are ignored
    (logged at debug level) since not every module is loaded in every config.

    min_permission is DEPRECATED — permission_level was fully retired in
    favor of named boolean capabilities (see TinyCTX/permissions.py and
    docs/PERMISSIONS-PLAN.md). The field is kept here only so old configs
    still parse; tool_handling/handler.py's apply_overrides() logs one
    warning per stale override naming the tool and pointing at
    permissions.templates, then ignores the value — it is NOT silently
    honored, and it is NOT a no-op that could look like it's still working.
    """
    always_on:      bool | None = None
    min_permission: int | None  = None


@dataclass
class ToolPassiveConfig:
    """
    Automatic, un-requested tool enabling — run once per user turn, before the
    model sees its tool list, over the same tools_vector_cache.db corpus that
    the explicit tools_search tool (see ToolSearchConfig) reads from.

    Up to `auto_limit` tools scoring above `auto_min_score` are enabled for
    that turn only; the enable is not persisted to session state, so the next
    turn re-runs the search fresh against its own query.

    Configured via 'tools.passive:' in config.yaml:

        tools:
          passive:
            auto_bm25_enabled: true
            auto_vector_enabled: false
            embedding_model: ""
            auto_limit: 2
            auto_min_score: 0.0
            rrf_k: 60

    auto_bm25_enabled: true  (default)
        Every turn, BM25-rank the full tool corpus against the user's message
        and auto-enable the top `auto_limit` hits. No network call, no
        embedding_model dependency — this is today's tools_search fuzzy-match
        logic, just run automatically instead of waiting for the model to
        call it.

    auto_vector_enabled: false  (default)
        Also embed the user's message and RRF-fuse vector similarity with the
        BM25 ranks (same fusion as modules/memory's search_memory). Requires
        embedding_model to name a 'kind: embedding' entry under models: — if
        unset, or the embed call fails, this silently falls back to
        auto_bm25_enabled's behavior. Off by default: unlike BM25 this costs
        a real embed() call every turn.

    embedding_model
        Model name to embed with when auto_vector_enabled is true. Resolved
        via Config.get_embedding_model() at use time, not at config load —
        intentionally independent from tools.search.embedding_model so
        passive (every turn) and explicit search (model-initiated, rarer)
        can point at different models.
    """
    auto_bm25_enabled:   bool  = True
    auto_vector_enabled: bool  = False
    embedding_model:     str   = ""
    auto_limit:          int   = 2
    auto_min_score:       float = 0.0
    rrf_k:                int   = 60


@dataclass
class ToolSearchConfig:
    """
    Config for the explicit, model-invoked tools_search tool — distinct from
    ToolPassiveConfig's automatic per-turn pass. The model deliberately calls
    this, so it can afford to be more thorough; it still only lists
    candidates, it does not auto-enable them (the model must call it again
    with an exact tool name to enable one).

    Configured via 'tools.search:' in config.yaml:

        tools:
          search:
            vector_enabled: false
            embedding_model: ""
            top_k: 5
            rrf_k: 60
            min_score: 0.0

    vector_enabled: false  (default)
        Whether tools_search's fuzzy (non-exact-name) path also uses vector
        similarity, RRF-fused with BM25, instead of BM25 alone. Requires
        embedding_model. Reads the same tools_vector_cache.db corpus as
        ToolPassiveConfig — tool descriptions are embedded once and shared
        between the two entry points, never twice.

    embedding_model
        Independent from tools.passive.embedding_model — see that field's
        docstring for why they're kept separate.

    top_k
        Max candidates listed per fuzzy search call.

    min_score
        Floor below which a candidate isn't listed at all, so a query that
        barely matches anything doesn't dump the whole registry back at the
        model.
    """
    vector_enabled:   bool  = False
    embedding_model:  str   = ""
    top_k:            int   = 5
    rrf_k:             int   = 60
    min_score:         float = 0.0


@dataclass
class ToolsConfig:
    """
    Top-level 'tools:' key in config.yaml — groups per-tool overrides and the
    two tool-discovery config blocks:

        tools:
          overrides: {}
          passive: {}
          search: {}

    See ToolOverrideConfig, ToolPassiveConfig, ToolSearchConfig for each
    sub-key's fields and defaults.
    """
    overrides: dict[str, ToolOverrideConfig] = field(default_factory=dict)
    passive:   ToolPassiveConfig             = field(default_factory=ToolPassiveConfig)
    search:    ToolSearchConfig              = field(default_factory=ToolSearchConfig)


@dataclass
class AttachmentConfig:
    """
    Thresholds that control whether attachments are inlined into the
    LLM message or saved to workspace/uploads/ with a reference note.

    Configured via the top-level 'attachments:' key in config.yaml:

        attachments:
          inline_max_files: 3        # max number of files to inline per message
          inline_max_bytes: 204800   # max total bytes to inline (~200 KB)
          uploads_dir: uploads       # relative to workspace root
    """
    inline_max_files: int = 3
    inline_max_bytes: int = 200 * 1024   # 200 KB
    uploads_dir:      str = "uploads"


@dataclass
class FallbackOnConfig:
    """Controls when the fallback chain is triggered."""
    any_error:  bool       = False
    http_codes: list[int]  = field(default_factory=lambda: [429, 500, 502, 503, 504])


@dataclass
class LLMRoutingConfig:
    """llm: block — primary model + fallback chain."""
    primary:     str                  = "main"
    fallback:    list[str]            = field(default_factory=list)
    fallback_on: FallbackOnConfig     = field(default_factory=FallbackOnConfig)


@dataclass
class RouterConfig:
    """Internal TCP config for the session router (not user-facing)."""
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class BridgeConfig:
    enabled: bool = False
    options: dict = field(default_factory=dict)

    def __getattr__(self, name: str):
        try:
            return self.options[name]
        except KeyError:
            raise AttributeError(name)


@dataclass
class GatewayConfig:
    """
    HTTP/SSE API gateway config.

    Configured via the top-level 'gateway:' key in config.yaml:

        gateway:
          enabled: true
          host: 127.0.0.1
          port: 8085
          api_key: "your-secret-token"
    """
    enabled: bool = False
    host:    str  = "127.0.0.1"
    port:    int  = 8085
    api_key: str  = ""

    def __post_init__(self):
        # Inside a container, the gateway must bind to 0.0.0.0 so Docker can
        # forward the port. TINYCTX_GATEWAY_HOST overrides whatever config.yaml
        # says without requiring a container-specific config file.
        override = os.environ.get("TINYCTX_GATEWAY_HOST", "").strip()
        if override:
            self.host = override
        # TINYCTX_PORT lets `tinyctx start` assign a per-instance port (set
        # by onboard/start to avoid collisions between multiple instances)
        # without editing config.yaml.
        port_override = os.environ.get("TINYCTX_PORT", "").strip()
        if port_override:
            self.port = int(port_override)


@dataclass
class WorkspaceConfig:
    """
    Global workspace directory. All modules that need a persistent home on
    disk resolve their paths relative to this.

    Configured via the top-level 'workspace:' key in config.yaml:

        workspace:
          path: ~/.tinyctx/workspace

    Optional — load() defaults this to <instance>/workspace, where
    <instance> is config.yaml's own directory, so it rarely needs stating
    explicitly. The bare dataclass default below (~/.tinyctx) only applies
    when Config is constructed directly, bypassing load() (e.g. tests).

    In Docker the tinyctx user's home is /home/tinyctx, so ~ resolves
    naturally to the bind-mounted workspace. No env var override needed.
    """
    path: Path = field(default_factory=lambda: Path("~/.tinyctx").expanduser())

    def __post_init__(self):
        override = os.environ.get("TINYCTX_WORKSPACE_PATH", "").strip()
        if override:
            self.path = Path(override).resolve()
        else:
            self.path = Path(self.path).expanduser().resolve()  # ~ → /home/tinyctx in container, %USERPROFILE% on Windows


@dataclass
class DataConfig:
    """
    Internal data directory — agent.db, users.db, and the memory graph live
    here. Separate from workspace/ so the agent's own filesystem tools
    (view/write_file/grep) never see or touch its internals.

    Configured via the top-level 'data:' key in config.yaml:

        data:
          path: ~/.tinyctx/data

    Optional — load() defaults this to <instance>/data, where <instance>
    is config.yaml's own directory. The bare dataclass default below only
    applies when Config is constructed directly, bypassing load().
    """
    path: Path = field(default_factory=lambda: Path("~/.tinyctx/data").expanduser())

    def __post_init__(self):
        override = os.environ.get("TINYCTX_DATA_PATH", "").strip()
        if override:
            self.path = Path(override).resolve()
        else:
            self.path = Path(self.path).expanduser().resolve()


@dataclass
class LoggingConfig:
    level: str = "INFO"

    def __post_init__(self):
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.level.upper() not in valid:
            raise ValueError(f"Invalid log level '{self.level}'.")
        self.level = self.level.upper()


@dataclass
class Config:
    models:          dict[str, ModelConfig]
    llm:             LLMRoutingConfig
    router:          RouterConfig            = field(default_factory=RouterConfig)
    bridges:         dict[str, BridgeConfig] = field(default_factory=dict)
    gateway:         GatewayConfig           = field(default_factory=GatewayConfig)
    workspace:       WorkspaceConfig         = field(default_factory=WorkspaceConfig)
    data:            DataConfig              = field(default_factory=DataConfig)
    logging:         LoggingConfig           = field(default_factory=LoggingConfig)
    max_tool_cycles: int                     = 20
    parallel:        int                     = 3     # max concurrent LLM/embedding requests in flight
    embed_cache_size: int                    = 2048  # max entries kept in ai.py's in-memory embedding cache (LRU)
    token_fuzz:      float                   = 1.1   # multiplier applied to counted tokens to account for tokenizer inaccuracy
    attachments:     AttachmentConfig        = field(default_factory=AttachmentConfig)
    permissions:     PermissionsConfig       = field(default_factory=PermissionsConfig)
    tools:           ToolsConfig             = field(default_factory=ToolsConfig)
    # When True, AgentError events (LLM error, abort) are written into the
    # conversation as a node so the LLM can see, on its next turn, that its
    # previous turn errored out — instead of the error vanishing silently
    # once it's been relayed to the bridge/console. See agent.py's AgentCycle.run().
    error_introspection:   bool               = False
    # When True, slash-command usage (excluding /reset) is recorded into
    # session state and surfaced to the LLM as a system-ish note on its next
    # turn, so it's aware a command was run on its branch. See
    # utils/commands.py's CommandRegistry.dispatch() and agent.py's run().
    command_introspection: bool               = False
    # Catch-all for unknown top-level keys (e.g. mcp:, custom module config, etc.)
    # Modules access this via agent.config.extra.get("mcp", {})
    extra:           dict                    = field(default_factory=dict)

    def get_model_config(self, name: str) -> ModelConfig:
        """
        Resolve a model name to its ModelConfig.
        Falls back to the primary model if name is not found.
        Raises KeyError only if primary itself is missing.
        """
        if name in self.models:
            return self.models[name]
        primary = self.llm.primary
        if primary in self.models:
            return self.models[primary]
        raise KeyError(
            f"Model '{name}' not found and primary '{primary}' is also missing."
        )

    def get_embedding_model(self, name: str) -> ModelConfig:
        """
        Return a ModelConfig that must be kind='embedding'.
        Raises ValueError if the name resolves to a chat model.
        Raises KeyError if the name is not in models at all.
        """
        if name not in self.models:
            raise KeyError(f"Embedding model '{name}' is not defined under models:")
        cfg = self.models[name]
        if not cfg.is_embedding:
            raise ValueError(
                f"Model '{name}' has kind='{cfg.kind}', expected 'embedding'. "
                "Add 'kind: embedding' to its models: entry."
            )
        return cfg


def resolve_log_level(level: str | int | None, *, default: int = logging.WARNING) -> int:
    """Best-effort log-level resolver for bridge/runtime overrides."""
    if isinstance(level, int):
        return level
    if not level:
        return default
    if isinstance(level, str):
        return getattr(logging, level.upper(), default)
    return default


def _parse_fallback_on(raw: dict) -> FallbackOnConfig:
    return FallbackOnConfig(
        any_error=bool(raw.get("any_error", False)),
        http_codes=list(raw.get("http_codes", [429, 500, 502, 503, 504])),
    )


def _parse_tool_overrides(raw: dict) -> dict[str, ToolOverrideConfig]:
    overrides: dict[str, ToolOverrideConfig] = {}
    for tool_name, o in (raw or {}).items():
        if not isinstance(o, dict):
            raise ValueError(f"tools.overrides.{tool_name} must be a mapping")
        always_on = o.get("always_on")
        min_permission = o.get("min_permission")
        if always_on is not None:
            always_on = bool(always_on)
        if min_permission is not None:
            min_permission = int(min_permission)
        overrides[tool_name] = ToolOverrideConfig(
            always_on=always_on,
            min_permission=min_permission,
        )
    return overrides


def _parse_tool_passive(raw: dict) -> ToolPassiveConfig:
    return ToolPassiveConfig(
        auto_bm25_enabled=bool(raw.get("auto_bm25_enabled", True)),
        auto_vector_enabled=bool(raw.get("auto_vector_enabled", False)),
        embedding_model=str(raw.get("embedding_model", "")),
        auto_limit=int(raw.get("auto_limit", 2)),
        auto_min_score=float(raw.get("auto_min_score", 0.0)),
        rrf_k=int(raw.get("rrf_k", 60)),
    )


def _parse_tool_search(raw: dict) -> ToolSearchConfig:
    return ToolSearchConfig(
        vector_enabled=bool(raw.get("vector_enabled", False)),
        embedding_model=str(raw.get("embedding_model", "")),
        top_k=int(raw.get("top_k", 5)),
        rrf_k=int(raw.get("rrf_k", 60)),
        min_score=float(raw.get("min_score", 0.0)),
    )


def _parse_permission_set(template_name: str, raw: dict) -> frozenset[Permission]:
    """
    Parse one templates.<name> mapping into a frozenset of granted
    Permission members. Templates are authored by the operator, so unlike
    the runtime fallbacks in users/store.py (which must tolerate a stale
    per-user override), an unknown permission name here is a config bug and
    fails loudly at load time rather than being silently dropped.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"permissions.templates.{template_name} must be a mapping")
    granted: set[Permission] = set()
    for perm_name, value in raw.items():
        try:
            perm = Permission(perm_name)
        except ValueError:
            valid = ", ".join(sorted(p.value for p in Permission))
            raise ValueError(
                f"permissions.templates.{template_name}: unknown permission "
                f"{perm_name!r}. Valid names: {valid}"
            )
        if bool(value):
            granted.add(perm)
    return frozenset(granted)


def _parse_permissions(raw: dict) -> PermissionsConfig:
    raw = raw or {}
    templates = dict(_BUILTIN_TEMPLATES)
    for name, tmpl_raw in (raw.get("templates") or {}).items():
        templates[name] = _parse_permission_set(name, tmpl_raw)

    default_template = raw.get("default_template", "guest")
    if default_template not in templates:
        raise ValueError(
            f"permissions.default_template {default_template!r} is not defined "
            f"under permissions.templates (known: {sorted(templates)})"
        )

    return PermissionsConfig(
        minimal_tokens=bool(raw.get("minimal_tokens", False)),
        default_template=default_template,
        templates=templates,
    )


def _parse_tools(raw: dict) -> ToolsConfig:
    raw = raw or {}
    return ToolsConfig(
        overrides=_parse_tool_overrides(raw.get("overrides", {})),
        passive=_parse_tool_passive(raw.get("passive", {})),
        search=_parse_tool_search(raw.get("search", {})),
    )


def _parse_model(raw: dict, default_context: int = 16384) -> ModelConfig:
    if not raw.get("base_url"):
        raise ValueError("Model config missing required field: base_url")
    if not raw.get("model"):
        raise ValueError("Model config missing required field: model")
    kind = raw.get("kind", "chat").lower()
    if kind not in ("chat", "embedding"):
        raise ValueError(f"Model kind must be 'chat' or 'embedding', got '{kind}'")
    tokens_per_image_raw = raw.get("tokens_per_image")
    if tokens_per_image_raw is not None:
        tokens_per_image = int(tokens_per_image_raw)
        if tokens_per_image <= 0:
            raise ValueError(f"tokens_per_image must be > 0, got {tokens_per_image}")
    else:
        tokens_per_image = None
    reasoning_effort = raw.get("reasoning_effort")
    if reasoning_effort is not None and reasoning_effort not in ("low", "medium", "high"):
        raise ValueError(
            f"reasoning_effort must be 'low', 'medium', or 'high', got '{reasoning_effort}'"
        )

    budget_tokens = raw.get("budget_tokens")
    if budget_tokens is not None:
        budget_tokens = int(budget_tokens)
        if budget_tokens <= 0:
            raise ValueError(f"budget_tokens must be > 0, got {budget_tokens}")

    vision = bool(raw.get("vision", False))

    context = int(raw.get("context", default_context))
    if context <= 0:
        raise ValueError(f"context must be > 0, got {context}")

    return ModelConfig(
        model=raw["model"],
        base_url=raw["base_url"],
        kind=kind,
        api_key_env=raw.get("api_key_env", "ANTHROPIC_API_KEY"),
        max_tokens=int(raw.get("max_tokens", 2048)),
        temperature=float(raw.get("temperature", 0.7)),
        budget_tokens=budget_tokens,
        reasoning_effort=reasoning_effort,
        cache_prompts=bool(raw.get("cache_prompts", False)),
        vision=vision,
        tokens_per_image=tokens_per_image,
        context=context,
        query_template=raw.get("query_template", "{text}"),
        document_template=raw.get("document_template", "{text}"),
    )


# Known top-level keys — everything else goes into Config.extra
_KNOWN_KEYS = {
    "models", "llm", "router", "bridges", "gateway", "workspace", "data",
    "logging", "max_tool_cycles", "parallel", "token_fuzz", "attachments", "permissions",
    "tools", "context",  # "context" is the deprecated legacy top-level key
    "error_introspection", "command_introspection",
}


def load(path="config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    with p.open(encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}

    # ------------------------------------------------------------------ models
    models_raw = raw.get("models")
    if not models_raw:
        raise ValueError("Config missing required section: [models]")

    # Legacy fallback: pre-refactor configs set a single top-level `context:`
    # applying to all models. That key is deprecated in favor of per-model
    # `context:` under each models.<name> entry, but if present it's still
    # honored as the default for any model that doesn't set its own.
    legacy_context = raw.get("context")
    if legacy_context is not None:
        logger.warning(
            "config.yaml: top-level 'context:' is deprecated — set 'context:' "
            "per model under models.<name> instead. Using %r as the default "
            "for any model that doesn't specify its own.", legacy_context,
        )
        default_context = int(legacy_context)
    else:
        default_context = 16384

    models: dict[str, ModelConfig] = {}
    for name, m in models_raw.items():
        try:
            models[name] = _parse_model(m, default_context=default_context)
        except ValueError as exc:
            raise ValueError(f"models.{name}: {exc}") from exc

    # ------------------------------------------------------------------ llm routing
    chat_models = {n for n, m in models.items() if not m.is_embedding}

    llm_raw = raw.get("llm", {})
    primary = llm_raw.get("primary", next(iter(n for n in models if not models[n].is_embedding), None))
    if primary is None:
        raise ValueError("No chat models defined. At least one model without 'kind: embedding' is required.")
    if primary not in chat_models:
        raise ValueError(
            f"llm.primary '{primary}' is not a chat model. "
            "Embedding models cannot be used as the primary LLM."
        )

    fallback = list(llm_raw.get("fallback") or [])
    for name in fallback:
        if name not in chat_models:
            raise ValueError(
                f"llm.fallback entry '{name}' is either not defined or is an embedding model."
            )

    fallback_on = _parse_fallback_on(llm_raw.get("fallback_on", {}))
    llm = LLMRoutingConfig(primary=primary, fallback=fallback, fallback_on=fallback_on)

    # ------------------------------------------------------------------ workspace
    # Defaults to <instance>/workspace, where <instance> is config.yaml's own
    # directory — config.yaml, workspace/, and data/ are colocated under one
    # instance dir, so there's nothing to state explicitly in most configs.
    ws_raw = raw.get("workspace", {})
    ws_path_raw = ws_raw.get("path") or (p.resolve().parent / "workspace")
    try:
        ws_path = Path(ws_path_raw).expanduser()
    except RuntimeError:
        ws_path = Path("/data")
    workspace = WorkspaceConfig(path=ws_path)

    # ------------------------------------------------------------------ data
    # Internal data dir (agent.db, users.db, memory graph). Defaults to
    # <instance>/data (config.yaml's own directory), same reasoning as
    # workspace above.
    data_raw = raw.get("data", {})
    data_path_raw = data_raw.get("path") or (p.resolve().parent / "data")
    data = DataConfig(path=Path(data_path_raw))

    # ------------------------------------------------------------------ rest
    router_raw = raw.get("router", {})
    log_raw    = raw.get("logging", {})

    bridges: dict[str, BridgeConfig] = {}
    for name, br in raw.get("bridges", {}).items():
        if isinstance(br, dict):
            enabled = bool(br.get("enabled", False))
            # Support both flat keys and a nested 'options:' sub-key.
            # If an 'options' dict is present, use it directly; otherwise
            # collect all non-'enabled' keys as the options dict.
            if "options" in br and isinstance(br["options"], dict):
                options = br["options"]
            else:
                options = {k: v for k, v in br.items() if k != "enabled"}
            bridges[name] = BridgeConfig(enabled=enabled, options=options)

    # ------------------------------------------------------------------ gateway
    gw_raw  = raw.get("gateway", {})
    gateway = GatewayConfig(
        enabled=bool(gw_raw.get("enabled", False)),
        host=gw_raw.get("host", "127.0.0.1"),
        port=int(gw_raw.get("port", 8085)),
        api_key=gw_raw.get("api_key", ""),
    )

    # ------------------------------------------------------------------ attachments
    att_raw = raw.get("attachments", {})
    attachments = AttachmentConfig(
        inline_max_files=int(att_raw.get("inline_max_files", 3)),
        inline_max_bytes=int(att_raw.get("inline_max_bytes", 200 * 1024)),
        uploads_dir=att_raw.get("uploads_dir", "uploads"),
    )

    # ------------------------------------------------------------------ permissions
    permissions = _parse_permissions(raw.get("permissions", {}))

    # ------------------------------------------------------------------ parallel
    parallel = int(raw.get("parallel", 3))
    if parallel < 1:
        raise ValueError(f"parallel must be >= 1, got {parallel}")

    # ------------------------------------------------------------------ tools
    tools = _parse_tools(raw.get("tools", {}))

    # ------------------------------------------------------------------ extra
    extra = {k: v for k, v in raw.items() if k not in _KNOWN_KEYS}

    cfg = Config(
        models=models,
        llm=llm,
        router=RouterConfig(
            host=router_raw.get("host", "127.0.0.1"),
            port=int(router_raw.get("port", 8765)),
        ),
        bridges=bridges,
        gateway=gateway,
        workspace=workspace,
        data=data,
        logging=LoggingConfig(level=log_raw.get("level", "INFO")),
        max_tool_cycles=int(raw.get("max_tool_cycles", 20)),
        parallel=parallel,
        token_fuzz=float(raw.get("token_fuzz", 1.1)),
        attachments=attachments,
        permissions=permissions,
        tools=tools,
        error_introspection=bool(raw.get("error_introspection", False)),
        command_introspection=bool(raw.get("command_introspection", False)),
        extra=extra,
    )
    setattr(cfg, "_source_path", p.resolve())
    return cfg


def apply_logging(cfg: LoggingConfig, *, level_override: str | int | None = None) -> None:
    import structlog
    resolved_level = resolve_log_level(level_override or cfg.level, default=logging.INFO)

    logging.basicConfig(
        level=resolved_level,
        format="%(message)s",
        datefmt="%H:%M:%S",
    )

    for _noisy in (
        "discord.gateway", "discord.client", "discord.http", "discord.state",
        "pdfminer", "pdfminer.pdfinterp", "pdfminer.pdfpage", "pdfminer.psparser",
        "pdfminer.cmapdb", "pdfminer.converter", "pdfminer.layout",
        "pdfplumber", "PIL",
    ):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
        
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

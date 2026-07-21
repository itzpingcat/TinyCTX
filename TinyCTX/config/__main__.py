"""
config.py — Configuration loader.
Imports only from stdlib and PyYAML. Never imports from contracts or gateway.
"""
from __future__ import annotations
import logging, os
from dataclasses import dataclass, field
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)


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
    Controls how the permission system interacts with the LLM's tool list.

    Configured via the top-level 'permissions:' key in config.yaml:

        permissions:
          minimal_tokens: true

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
    """
    minimal_tokens: bool = False


@dataclass
class ToolOverrideConfig:
    """
    Per-tool override of registration-time defaults (always_on / min_permission).

    Configured via the top-level 'tool_overrides:' key in config.yaml:

        tool_overrides:
          shell:
            min_permission: 80
          present:
            always_on: true
          memory_search:
            always_on: false
            min_permission: 10

    Fields left unset (null/omitted) leave that aspect of the tool untouched —
    only the fields you specify are overridden. Unknown tool names are ignored
    (logged at debug level) since not every module is loaded in every config.
    """
    always_on:      bool | None = None
    min_permission: int | None  = None


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
    token_fuzz:      float                   = 1.1   # multiplier applied to counted tokens to account for tokenizer inaccuracy
    attachments:     AttachmentConfig        = field(default_factory=AttachmentConfig)
    permissions:     PermissionsConfig       = field(default_factory=PermissionsConfig)
    tool_overrides:  dict[str, ToolOverrideConfig] = field(default_factory=dict)
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
            raise ValueError(f"tool_overrides.{tool_name} must be a mapping")
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
    )


# Known top-level keys — everything else goes into Config.extra
_KNOWN_KEYS = {
    "models", "llm", "router", "bridges", "gateway", "workspace", "data",
    "logging", "max_tool_cycles", "parallel", "token_fuzz", "attachments", "permissions",
    "tool_overrides", "context",  # "context" is the deprecated legacy top-level key
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
    perm_raw = raw.get("permissions", {})
    permissions = PermissionsConfig(
        minimal_tokens=bool(perm_raw.get("minimal_tokens", False)),
    )

    # ------------------------------------------------------------------ parallel
    parallel = int(raw.get("parallel", 3))
    if parallel < 1:
        raise ValueError(f"parallel must be >= 1, got {parallel}")

    # ------------------------------------------------------------------ tool_overrides
    tool_overrides = _parse_tool_overrides(raw.get("tool_overrides", {}))

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
        tool_overrides=tool_overrides,
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

"""
config.py — Configuration loader.
Imports only from stdlib and PyYAML. Never imports from contracts or gateway.
"""
from __future__ import annotations
import logging, os
from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass
class ModelConfig:
    """One named model entry under models:"""
    model:       str
    base_url:    str
    api_key_env: str  = "ANTHROPIC_API_KEY"
    max_tokens:  int  = 2048
    temperature: float = 0.7

    @property
    def api_key(self) -> str:
        if not self.api_key_env or self.api_key_env.upper() == "N/A":
            return ""
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise EnvironmentError(
                f"LLM API key not set. Export {self.api_key_env} before starting."
            )
        return key


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
class GatewayConfig:
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
class MemoryConfig:
    workspace_path: Path = field(default_factory=lambda: Path("~/.tinyctx").expanduser())

    def __post_init__(self):
        self.workspace_path = Path(self.workspace_path).expanduser().resolve()


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
    gateway:         GatewayConfig         = field(default_factory=GatewayConfig)
    bridges:         dict[str, BridgeConfig] = field(default_factory=dict)
    memory:          MemoryConfig           = field(default_factory=MemoryConfig)
    logging:         LoggingConfig          = field(default_factory=LoggingConfig)
    max_tool_cycles: int                    = 10
    context:         int                    = 16384
    # Catch-all for unknown top-level keys (e.g. mcp:, custom module config, etc.)
    # Modules access this via agent.config.extra.get("mcp", {})
    extra:           dict                   = field(default_factory=dict)

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


def _parse_fallback_on(raw: dict) -> FallbackOnConfig:
    return FallbackOnConfig(
        any_error=bool(raw.get("any_error", False)),
        http_codes=list(raw.get("http_codes", [429, 500, 502, 503, 504])),
    )


def _parse_model(raw: dict) -> ModelConfig:
    if not raw.get("base_url"):
        raise ValueError("Model config missing required field: base_url")
    if not raw.get("model"):
        raise ValueError("Model config missing required field: model")
    return ModelConfig(
        model=raw["model"],
        base_url=raw["base_url"],
        api_key_env=raw.get("api_key_env", "ANTHROPIC_API_KEY"),
        max_tokens=int(raw.get("max_tokens", 2048)),
        temperature=float(raw.get("temperature", 0.7)),
    )


# Known top-level keys — everything else goes into Config.extra
_KNOWN_KEYS = {
    "models", "llm", "gateway", "bridges", "memory",
    "logging", "max_tool_cycles", "context",
}


def load(path="config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    with p.open() as f:
        raw = yaml.safe_load(f) or {}

    # ------------------------------------------------------------------ models
    models_raw = raw.get("models")
    if not models_raw:
        raise ValueError("Config missing required section: [models]")

    models: dict[str, ModelConfig] = {}
    for name, m in models_raw.items():
        try:
            models[name] = _parse_model(m)
        except ValueError as exc:
            raise ValueError(f"models.{name}: {exc}") from exc

    # ------------------------------------------------------------------ llm routing
    llm_raw = raw.get("llm", {})
    primary = llm_raw.get("primary", next(iter(models)))
    if primary not in models:
        raise ValueError(f"llm.primary '{primary}' is not defined under models:")

    fallback = list(llm_raw.get("fallback") or [])
    for name in fallback:
        if name not in models:
            raise ValueError(f"llm.fallback entry '{name}' is not defined under models:")

    fallback_on = _parse_fallback_on(llm_raw.get("fallback_on", {}))

    llm = LLMRoutingConfig(
        primary=primary,
        fallback=fallback,
        fallback_on=fallback_on,
    )

    # ------------------------------------------------------------------ rest
    gw_raw  = raw.get("gateway", {})
    mem_raw = raw.get("memory", {})
    log_raw = raw.get("logging", {})

    bridges: dict[str, BridgeConfig] = {}
    for name, br in raw.get("bridges", {}).items():
        if isinstance(br, dict):
            enabled = bool(br.get("enabled", False))
            options = {k: v for k, v in br.items() if k != "enabled"}
            bridges[name] = BridgeConfig(enabled=enabled, options=options)

    # ------------------------------------------------------------------ extra
    extra = {k: v for k, v in raw.items() if k not in _KNOWN_KEYS}

    return Config(
        models=models,
        llm=llm,
        gateway=GatewayConfig(
            host=gw_raw.get("host", "127.0.0.1"),
            port=int(gw_raw.get("port", 8765)),
        ),
        bridges=bridges,
        memory=MemoryConfig(
            workspace_path=Path(mem_raw.get("workspace_path", "~/.tinyctx"))
        ),
        logging=LoggingConfig(level=log_raw.get("level", "INFO")),
        max_tool_cycles=int(raw.get("max_tool_cycles", 10)),
        context=int(raw.get("context", 16384)),
        extra=extra,
    )


def apply_logging(cfg: LoggingConfig) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
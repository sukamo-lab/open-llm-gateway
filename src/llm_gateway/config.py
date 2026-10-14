from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limit(StrictModel):
    requests: int | None = Field(default=None, gt=0)
    tokens: int | None = Field(default=None, gt=0)
    cost_usd: float | None = Field(default=None, gt=0)
    window_seconds: int = Field(default=60, gt=0)

    @model_validator(mode="after")
    def has_dimension(self) -> "Limit":
        if self.requests is None and self.tokens is None and self.cost_usd is None:
            raise ValueError("a limit must set requests, tokens, or cost_usd")
        return self


class ModelConfig(StrictModel):
    name: str
    upstream_model: str
    reasoning: bool = False
    priority: int = 100
    capabilities: set[Literal["chat", "reasoning", "vision", "tools", "json"]] = Field(
        default_factory=lambda: {"chat", "vision", "tools", "json"}
    )
    input_cost_per_million: float = Field(default=0, ge=0)
    output_cost_per_million: float = Field(default=0, ge=0)
    request_overrides: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_capabilities(self) -> "ModelConfig":
        if self.reasoning:
            self.capabilities.add("reasoning")
        return self


class ProviderConfig(StrictModel):
    name: str
    base_url: str
    chat_completions_path: str = "/chat/completions"
    api_key_env: str | None = None
    api_key_file: str | None = None
    api_key_value: str | None = None
    api_key_header: str = "Authorization"
    api_key_prefix: str = "Bearer"
    tier: Literal["free", "paid"] = "free"
    enabled: bool = True
    timeout_seconds: float = Field(default=120, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)
    request_defaults: dict[str, object] = Field(default_factory=dict)
    limits: list[Limit] = Field(default_factory=list)
    max_concurrency: int = Field(default=10, gt=0)
    failure_threshold: int = Field(default=3, gt=0)
    cooldown_seconds: int = Field(default=60, ge=1)
    models: list[ModelConfig]

    def api_key(self) -> str | None:
        if self.api_key_env:
            value = os.getenv(self.api_key_env)
            if value:
                return value
        if self.api_key_file:
            try:
                value = Path(self.api_key_file).read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
        if self.api_key_value and not self.api_key_value.startswith("PASTE_YOUR_"):
            return self.api_key_value
        return None

    @model_validator(mode="after")
    def has_api_key_source(self) -> "ProviderConfig":
        if not self.api_key_env and not self.api_key_file and not self.api_key_value:
            raise ValueError("set api_key_env, api_key_file, or api_key_value")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must start with http:// or https://")
        names = [model.name for model in self.models]
        if len(names) != len(set(names)):
            raise ValueError("model aliases must be unique within a provider")
        return self


class GatewayConfig(StrictModel):
    mode: Literal["single", "multi"] = "single"
    database: str = "./data/gateway.db"
    server: "ServerConfig" = Field(default_factory=lambda: ServerConfig())
    auth: "AuthConfig" = Field(default_factory=lambda: AuthConfig())
    routing: "RoutingConfig" = Field(default_factory=lambda: RoutingConfig())
    observability: "ObservabilityConfig" = Field(default_factory=lambda: ObservabilityConfig())
    accounting: "AccountingConfig" = Field(default_factory=lambda: AccountingConfig())
    idempotency: "IdempotencyConfig" = Field(default_factory=lambda: IdempotencyConfig())
    cache: "CacheConfig" = Field(default_factory=lambda: CacheConfig())
    telemetry: "TelemetryConfig" = Field(default_factory=lambda: TelemetryConfig())
    features: "FeatureConfig" = Field(default_factory=lambda: FeatureConfig())
    providers: list[ProviderConfig]

    @model_validator(mode="after")
    def unique_providers(self) -> "GatewayConfig":
        names = [provider.name for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        if not self.providers:
            raise ValueError("at least one provider is required")
        defaults = {
            "single": {
                "authentication": False, "admin_api": False, "managed_keys": False,
                "tenant_quotas": False, "provider_quotas": False,
                "usage_accounting": False, "cost_tracking": False,
                "metrics": False, "circuit_breaker": True, "structured_logging": False,
                "idempotency": False,
                "exact_cache": False,
                "tracing": False,
            },
            "multi": {
                "authentication": True, "admin_api": True, "managed_keys": True,
                "tenant_quotas": True, "provider_quotas": True,
                "usage_accounting": True, "cost_tracking": True,
                "metrics": True, "circuit_breaker": True, "structured_logging": True,
                "idempotency": True,
                "exact_cache": False,
                "tracing": False,
            },
        }[self.mode]
        for name, default in defaults.items():
            if getattr(self.features, name) is None:
                object.__setattr__(self.features, name, default)
        if self.features.managed_keys and not self.features.authentication:
            raise ValueError("managed_keys requires authentication")
        if self.features.managed_keys and not self.features.admin_api:
            raise ValueError("managed_keys requires admin_api")
        if self.features.tenant_quotas and not self.features.managed_keys:
            raise ValueError("tenant_quotas requires managed_keys")
        if self.features.cost_tracking and not self.features.usage_accounting:
            raise ValueError("cost_tracking requires usage_accounting")
        if self.features.exact_cache and self.cache.backend == "redis" and not self.cache.redis_url():
            raise ValueError(f"exact_cache Redis backend requires {self.cache.redis_url_env}")
        return self

    def diagnostics(self) -> list[tuple[str, str]]:
        issues: list[tuple[str, str]] = []
        configured = [p for p in self.providers if p.enabled and p.api_key()]
        if not configured:
            issues.append(("ERROR", "no enabled provider has usable credentials"))
        if not self.features.authentication:
            if self.mode == "multi":
                issues.append(("ERROR", "multi-user mode requires authentication"))
            if self.server.host not in {"127.0.0.1", "localhost", "::1"}:
                issues.append(("ERROR", "unauthenticated gateway must bind only to localhost"))
        elif not self.auth.admin_key():
            issues.append(("ERROR", f"{self.auth.admin_key_env} is not set"))
        elif len(self.auth.admin_key() or "") < 24:
            issues.append(("ERROR", "administrator key must be at least 24 characters"))
        for key in self.auth.api_keys():
            if len(key) < 24 or key.lower().startswith(("change", "client-key", "example")):
                issues.append(("ERROR", "a bootstrap client key is weak or still a placeholder"))
        if self.mode == "multi" and not self.database.startswith(("postgresql://", "postgres://")):
            issues.append(("WARN", "SQLite supports only a single gateway instance"))
        if self.mode == "multi" and self.routing.allow_provider_override:
            issues.append(("WARN", "provider override is enabled"))
        if self.mode == "multi" and self.server.docs_enabled:
            issues.append(("WARN", "interactive API documentation is enabled"))
        if "*" in self.server.cors_origins:
            issues.append(("WARN", "CORS allows every origin"))
        if self.mode == "multi" and self.observability.include_upstream_errors:
            issues.append(("WARN", "upstream error details may be returned to clients"))
        if self.mode == "multi" and self.features.exact_cache and self.cache.backend == "memory":
            issues.append(("WARN", "in-memory exact cache is not shared across replicas"))
        endpoint = self.telemetry.endpoint()
        if self.features.tracing and endpoint and endpoint.startswith("http://"):
            issues.append(("WARN", "OpenTelemetry exporter endpoint is not encrypted"))
        for provider in self.providers:
            if self.mode == "multi" and provider.enabled and provider.api_key_value and provider.api_key():
                issues.append(("WARN", f"{provider.name} stores its API key directly in YAML"))
            if provider.enabled and provider.base_url.startswith("http://"):
                issues.append(("WARN", f"{provider.name} uses an unencrypted upstream URL"))
        return issues


class ServerConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)
    cors_origins: list[str] = Field(default_factory=list)
    docs_enabled: bool = True
    max_request_body_bytes: int = Field(default=10_485_760, ge=1024)


class AuthConfig(StrictModel):
    api_keys_env: str = "LLM_GATEWAY_API_KEYS"
    api_keys_file: str | None = None
    admin_key_env: str = "LLM_GATEWAY_ADMIN_KEY"
    admin_key_file: str | None = None
    key_cache_seconds: int = Field(default=60, ge=0, le=3600)
    key_cache_max_entries: int = Field(default=10_000, ge=0, le=1_000_000)

    def api_keys(self) -> set[str]:
        values = os.getenv(self.api_keys_env, "")
        if self.api_keys_file:
            try:
                values += "," + Path(self.api_keys_file).read_text(encoding="utf-8")
            except OSError:
                pass
        return {value.strip() for value in values.replace("\n", ",").split(",") if value.strip()}

    def admin_key(self) -> str | None:
        value = os.getenv(self.admin_key_env)
        if value:
            return value
        if self.admin_key_file:
            try:
                return Path(self.admin_key_file).read_text(encoding="utf-8").strip() or None
            except OSError:
                pass
        return None


class RoutingConfig(StrictModel):
    max_attempts: int = Field(default=0, ge=0)
    allow_provider_override: bool = False
    paid_fallback: bool = True
    http_max_connections: int = Field(default=200, gt=0)
    http_max_keepalive_connections: int = Field(default=50, ge=0)
    http_keepalive_expiry_seconds: float = Field(default=30, gt=0)
    retry_attempts: int = Field(default=1, ge=1, le=10)
    retry_initial_backoff_seconds: float = Field(default=0.25, ge=0, le=60)
    retry_max_backoff_seconds: float = Field(default=8, ge=0, le=300)
    retry_jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    request_deadline_seconds: float | None = Field(default=None, gt=0, le=3600)

    @model_validator(mode="after")
    def valid_retry_backoff(self) -> "RoutingConfig":
        if self.retry_max_backoff_seconds < self.retry_initial_backoff_seconds:
            raise ValueError("retry_max_backoff_seconds must be at least retry_initial_backoff_seconds")
        return self


class ObservabilityConfig(StrictModel):
    include_upstream_errors: bool = True
    log_requests: bool = True
    log_provider_attempts: bool = True
    log_health_checks: bool = False
    include_exception_stacktraces: bool = False


class AccountingConfig(StrictModel):
    retention_days: int = Field(default=90, ge=30)


class IdempotencyConfig(StrictModel):
    ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    lease_seconds: int = Field(default=180, ge=5, le=3600)
    wait_timeout_seconds: float = Field(default=30, ge=0, le=300)
    poll_interval_seconds: float = Field(default=0.1, gt=0, le=5)
    max_response_bytes: int = Field(default=10_485_760, ge=1024, le=67_108_864)
    min_key_length: int = Field(default=8, ge=1, le=128)
    max_key_length: int = Field(default=255, ge=8, le=1024)

    @model_validator(mode="after")
    def valid_key_lengths(self) -> "IdempotencyConfig":
        if self.max_key_length < self.min_key_length:
            raise ValueError("max_key_length must be at least min_key_length")
        return self


class CacheConfig(StrictModel):
    backend: Literal["memory", "database", "redis"] = "memory"
    ttl_seconds: int = Field(default=3600, ge=1, le=604800)
    max_entries: int = Field(default=10_000, ge=1, le=10_000_000)
    max_response_bytes: int = Field(default=10_485_760, ge=1024, le=67_108_864)
    namespace: str = Field(default="v1", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    allow_client_bypass: bool = True
    failure_mode: Literal["open", "closed"] = "open"
    redis_url_env: str = "LLM_GATEWAY_REDIS_URL"
    redis_prefix: str = Field(default="llm-gateway:exact", min_length=1, max_length=128)

    def redis_url(self) -> str | None:
        return os.getenv(self.redis_url_env) or None


class TelemetryConfig(StrictModel):
    endpoint_env: str = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    sample_ratio: float = Field(default=1.0, ge=0, le=1)
    export_timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_queue_size: int = Field(default=2048, ge=1, le=100_000)
    max_export_batch_size: int = Field(default=512, ge=1, le=10_000)
    schedule_delay_millis: int = Field(default=5000, ge=100, le=60_000)
    excluded_paths: set[str] = Field(default_factory=lambda: {"/health", "/ready"})
    include_tenant_id: bool = False
    failure_mode: Literal["open", "closed"] = "open"

    def endpoint(self) -> str | None:
        return os.getenv(self.endpoint_env) or None

    @model_validator(mode="after")
    def valid_batch_size(self) -> "TelemetryConfig":
        if self.max_export_batch_size > self.max_queue_size:
            raise ValueError("max_export_batch_size must not exceed max_queue_size")
        return self


class FeatureConfig(StrictModel):
    authentication: bool | None = None
    admin_api: bool | None = None
    managed_keys: bool | None = None
    tenant_quotas: bool | None = None
    provider_quotas: bool | None = None
    usage_accounting: bool | None = None
    cost_tracking: bool | None = None
    metrics: bool | None = None
    circuit_breaker: bool | None = None
    structured_logging: bool | None = None
    idempotency: bool | None = None
    exact_cache: bool | None = None
    tracing: bool | None = None


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value):
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ValueError(f"environment variable {name} is required by the configuration")
        return _ENV_PATTERN.sub(replace, value)
    return value


def _merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, value in override.items():
            result[key] = _merge(result[key], value) if key in result else value
        return result
    if isinstance(base, list) and isinstance(override, list):
        return [*base, *override]
    return override


def _load_yaml(path: Path, seen: set[Path] | None = None):
    resolved = path.resolve()
    seen = set() if seen is None else seen
    if resolved in seen:
        raise ValueError(f"configuration include cycle at {resolved}")
    seen.add(resolved)
    data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    includes = data.pop("include", [])
    if isinstance(includes, str):
        includes = [includes]
    merged = {}
    for pattern in includes:
        matches = sorted(resolved.parent.glob(pattern))
        if not matches:
            raise ValueError(f"configuration include matched no files: {pattern}")
        for included in matches:
            merged = _merge(merged, _load_yaml(included, seen))
    seen.remove(resolved)
    return _merge(merged, data)


def load_config(path: str | Path) -> GatewayConfig:
    GatewayConfig.model_rebuild()
    config_path = Path(path).resolve()
    data = _expand_env(_load_yaml(config_path))
    database = data.get("database")
    if isinstance(database, str) and not database.startswith(("postgresql://", "postgres://")):
        data["database"] = str((config_path.parent / database).resolve())
    auth = data.get("auth", {})
    for field in ("api_keys_file", "admin_key_file"):
        if auth.get(field):
            auth[field] = str((config_path.parent / auth[field]).resolve())
    for provider in data.get("providers", []):
        if provider.get("api_key_file"):
            provider["api_key_file"] = str((config_path.parent / provider["api_key_file"]).resolve())
    return GatewayConfig.model_validate(data)


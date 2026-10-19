from __future__ import annotations

import math
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
    capabilities: set[Literal[
        "chat", "reasoning", "vision", "tools", "json", "embeddings", "audio_speech",
        "image_generation",
    ]] = Field(
        default_factory=lambda: {"chat", "vision", "tools", "json"}
    )
    input_cost_per_million: float = Field(default=0, ge=0)
    output_cost_per_million: float = Field(default=0, ge=0)
    pricing_version: str | None = Field(default=None, min_length=1, max_length=64)
    request_overrides: dict[str, object] = Field(default_factory=dict)
    prompt_injection_safe: bool = False
    quality: int = Field(default=50, ge=0, le=100)

    @model_validator(mode="after")
    def normalize_capabilities(self) -> "ModelConfig":
        if self.reasoning:
            self.capabilities.add("reasoning")
        return self


class ProviderCredentialConfig(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    api_key_env: str | None = None
    api_key_file: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def one_source(self) -> "ProviderCredentialConfig":
        if bool(self.api_key_env) == bool(self.api_key_file):
            raise ValueError("provider credential requires exactly one environment or file source")
        return self

    def value(self) -> str | None:
        if not self.enabled:
            return None
        if self.api_key_env:
            return os.getenv(self.api_key_env) or None
        try:
            return Path(self.api_key_file or "").read_text(encoding="utf-8").strip() or None
        except OSError:
            return None


class ProviderConfig(StrictModel):
    name: str
    base_url: str
    chat_completions_path: str = "/chat/completions"
    embeddings_path: str = "/embeddings"
    audio_speech_path: str = "/audio/speech"
    image_generations_path: str = "/images/generations"
    models_path: str = "/models"
    discovery_enabled: bool = True
    api_key_env: str | None = None
    api_key_file: str | None = None
    api_key_value: str | None = None
    credentials: list[ProviderCredentialConfig] = Field(default_factory=list, max_length=10)
    api_key_header: str = "Authorization"
    api_key_prefix: str = "Bearer"
    tier: Literal["free", "paid"] = "free"
    region: str | None = Field(default=None, min_length=1, max_length=64)
    residencies: set[str] = Field(default_factory=set, max_length=32)
    enabled: bool = True
    timeout_seconds: float = Field(default=120, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)
    request_defaults: dict[str, object] = Field(default_factory=dict)
    embeddings_request_defaults: dict[str, object] = Field(default_factory=dict)
    audio_speech_request_defaults: dict[str, object] = Field(default_factory=dict)
    image_request_defaults: dict[str, object] = Field(default_factory=dict)
    limits: list[Limit] = Field(default_factory=list)
    max_concurrency: int = Field(default=10, gt=0)
    failure_threshold: int = Field(default=3, gt=0)
    cooldown_seconds: int = Field(default=60, ge=1)
    models: list[ModelConfig]

    def api_key(self) -> str | None:
        configured = self.credential_values()
        if configured:
            return configured[0][1]
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

    def credential_values(self) -> list[tuple[str, str]]:
        return [(item.id, value) for item in self.credentials if (value := item.value())]

    @model_validator(mode="after")
    def has_api_key_source(self) -> "ProviderConfig":
        if not self.credentials and not self.api_key_env and not self.api_key_file and not self.api_key_value:
            raise ValueError("set api_key_env, api_key_file, or api_key_value")
        credential_ids = [item.id for item in self.credentials]
        if len(credential_ids) != len(set(credential_ids)):
            raise ValueError("provider credential ids must be unique")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must start with http:// or https://")
        names = [model.name for model in self.models]
        if len(names) != len(set(names)):
            raise ValueError("model aliases must be unique within a provider")
        if self.region is not None and not self.region.strip():
            raise ValueError("provider region must not be blank")
        if any(not value or len(value) > 128 for value in self.residencies):
            raise ValueError("provider residencies must be bounded non-empty strings")
        return self


class GatewayConfig(StrictModel):
    mode: Literal["single", "multi"] = "single"
    database: str = "./data/gateway.db"
    server: "ServerConfig" = Field(default_factory=lambda: ServerConfig())
    auth: "AuthConfig" = Field(default_factory=lambda: AuthConfig())
    routing: "RoutingConfig" = Field(default_factory=lambda: RoutingConfig())
    observability: "ObservabilityConfig" = Field(default_factory=lambda: ObservabilityConfig())
    accounting: "AccountingConfig" = Field(default_factory=lambda: AccountingConfig())
    finops: "FinOpsConfig" = Field(default_factory=lambda: FinOpsConfig())
    jobs: "JobsConfig" = Field(default_factory=lambda: JobsConfig())
    batch: "BatchConfig" = Field(default_factory=lambda: BatchConfig())
    coordination: "CoordinationConfig" = Field(default_factory=lambda: CoordinationConfig())
    semantic_cache: "SemanticCacheConfig" = Field(default_factory=lambda: SemanticCacheConfig())
    idempotency: "IdempotencyConfig" = Field(default_factory=lambda: IdempotencyConfig())
    cache: "CacheConfig" = Field(default_factory=lambda: CacheConfig())
    telemetry: "TelemetryConfig" = Field(default_factory=lambda: TelemetryConfig())
    pii: "PIIConfig" = Field(default_factory=lambda: PIIConfig())
    moderation: "ModerationConfig" = Field(default_factory=lambda: ModerationConfig())
    prompt_injection: "PromptInjectionConfig" = Field(default_factory=lambda: PromptInjectionConfig())
    responses: "ResponsesConfig" = Field(default_factory=lambda: ResponsesConfig())
    embeddings: "EmbeddingsConfig" = Field(default_factory=lambda: EmbeddingsConfig())
    moderations_api: "ModerationsAPIConfig" = Field(default_factory=lambda: ModerationsAPIConfig())
    audio: "AudioConfig" = Field(default_factory=lambda: AudioConfig())
    images: "ImagesConfig" = Field(default_factory=lambda: ImagesConfig())
    model_discovery: "ModelDiscoveryConfig" = Field(default_factory=lambda: ModelDiscoveryConfig())
    config_reload: "ConfigReloadConfig" = Field(default_factory=lambda: ConfigReloadConfig())
    oidc: "OIDCConfig" = Field(default_factory=lambda: OIDCConfig())
    audit: "AuditConfig" = Field(default_factory=lambda: AuditConfig())
    encryption: "EncryptionConfig" = Field(default_factory=lambda: EncryptionConfig())
    residency: "ResidencyConfig" = Field(default_factory=lambda: ResidencyConfig())
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
                "identity_hierarchy": False,
                "rbac": False,
                "oidc": False,
                "audit_ledger": False,
                "envelope_encryption": False,
                "residency_enforcement": False,
                "finops_plans": False,
                "finops_export": False,
                "async_jobs": False,
                "batch_api": False,
                "distributed_resilience": False,
                "semantic_cache": False,
                "tenant_quotas": False, "provider_quotas": False,
                "usage_accounting": False, "cost_tracking": False,
                "metrics": False, "circuit_breaker": True, "structured_logging": False,
                "idempotency": False,
                "exact_cache": False,
                "tracing": False,
                "pii_detection": False,
                "moderation": False,
                "prompt_injection": False,
                "responses_api": False,
                "embeddings_api": False,
                "moderations_api": False,
                "audio_api": False,
                "images_api": False,
                "model_discovery": False,
                "cost_routing": False,
                "latency_routing": False,
                "routing_policy": False,
                "config_reload": False,
            },
            "multi": {
                "authentication": True, "admin_api": True, "managed_keys": True,
                "identity_hierarchy": False,
                "rbac": False,
                "oidc": False,
                "audit_ledger": False,
                "envelope_encryption": False,
                "residency_enforcement": False,
                "finops_plans": False,
                "finops_export": False,
                "async_jobs": False,
                "batch_api": False,
                "distributed_resilience": False,
                "semantic_cache": False,
                "tenant_quotas": True, "provider_quotas": True,
                "usage_accounting": True, "cost_tracking": True,
                "metrics": True, "circuit_breaker": True, "structured_logging": True,
                "idempotency": True,
                "exact_cache": False,
                "tracing": False,
                "pii_detection": False,
                "moderation": False,
                "prompt_injection": False,
                "responses_api": False,
                "embeddings_api": False,
                "moderations_api": False,
                "audio_api": False,
                "images_api": False,
                "model_discovery": False,
                "cost_routing": False,
                "latency_routing": False,
                "routing_policy": False,
                "config_reload": False,
            },
        }[self.mode]
        for name, default in defaults.items():
            if getattr(self.features, name) is None:
                object.__setattr__(self.features, name, default)
        if self.features.managed_keys and not self.features.authentication:
            raise ValueError("managed_keys requires authentication")
        if self.features.managed_keys and not self.features.admin_api:
            raise ValueError("managed_keys requires admin_api")
        if self.features.identity_hierarchy and not (
            self.features.authentication and self.features.admin_api and self.features.managed_keys
        ):
            raise ValueError("identity_hierarchy requires authentication, admin_api, and managed_keys")
        if self.features.rbac and not self.features.identity_hierarchy:
            raise ValueError("rbac requires identity_hierarchy")
        if self.features.oidc and not self.features.rbac:
            raise ValueError("oidc requires rbac")
        if self.features.oidc and (not self.oidc.issuer or not self.oidc.audience):
            raise ValueError("oidc requires issuer and audience")
        if self.features.audit_ledger and not self.features.admin_api:
            raise ValueError("audit_ledger requires admin_api")
        if self.features.tenant_quotas and not self.features.managed_keys:
            raise ValueError("tenant_quotas requires managed_keys")
        if self.features.finops_plans and not (
            self.features.managed_keys and self.features.admin_api and self.features.tenant_quotas
        ):
            raise ValueError("finops_plans requires managed_keys, admin_api, and tenant_quotas")
        if self.features.finops_plans and not self.finops.plans:
            raise ValueError("finops_plans requires at least one configured plan")
        if self.features.finops_export and not (
            self.features.admin_api and self.features.usage_accounting and self.features.audit_ledger
        ):
            raise ValueError("finops_export requires admin_api, usage_accounting, and audit_ledger")
        if self.features.finops_export and not self.finops.export_url:
            raise ValueError("finops_export requires finops.export_url")
        if self.features.async_jobs and not (self.features.authentication and self.features.managed_keys):
            raise ValueError("async_jobs requires authentication and managed_keys")
        if self.features.async_jobs and self.jobs.webhook_url and not self.jobs.webhook_secret():
            raise ValueError(f"job webhook requires {self.jobs.webhook_secret_env}")
        if self.features.batch_api and not self.features.async_jobs:
            raise ValueError("batch_api requires async_jobs")
        if self.features.batch_api and not self.batch.allow_payload_persistence:
            raise ValueError("batch_api requires batch.allow_payload_persistence acknowledgement")
        if self.features.batch_api and (
            self.features.pii_detection
            or self.features.moderation
            or self.features.prompt_injection
        ):
            raise ValueError(
                "batch_api cannot be combined with request content governance in this release"
            )
        if self.features.distributed_resilience and not self.database.startswith(("postgresql://", "postgres://")):
            raise ValueError("distributed_resilience requires PostgreSQL")
        if self.features.semantic_cache and not self.features.embeddings_api:
            raise ValueError("semantic_cache requires embeddings_api")
        if (
            self.features.semantic_cache
            and not self.semantic_cache.allow_sensitive_content
            and not self.features.pii_detection
        ):
            raise ValueError("semantic_cache requires pii_detection or allow_sensitive_content")
        if self.features.cost_tracking and not self.features.usage_accounting:
            raise ValueError("cost_tracking requires usage_accounting")
        if self.features.model_discovery and not self.features.admin_api:
            raise ValueError("model_discovery requires admin_api")
        if self.features.config_reload and not self.features.admin_api:
            raise ValueError("config_reload requires admin_api")
        if self.features.routing_policy and not self.routing.policies:
            raise ValueError("routing_policy requires at least one routing policy")
        if self.features.residency_enforcement:
            if not self.features.routing_policy:
                raise ValueError("residency_enforcement requires routing_policy")
            policy = next((item for item in self.routing.policies if item.name == self.residency.policy), None)
            if policy is None or not policy.residencies or policy.fallback != "deny":
                raise ValueError("residency policy must exist, require residencies, and deny fallback")
        if self.features.config_reload and self.features.exact_cache:
            raise ValueError("config_reload cannot be combined with exact_cache")
        if self.features.cost_routing and self.features.latency_routing:
            raise ValueError("cost_routing and latency_routing are mutually exclusive")
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
        if self.features.pii_detection and self.pii.failure_mode == "open":
            issues.append(("WARN", "PII scanner failures are configured to bypass policy"))
        if (
            self.features.pii_detection
            and self.pii.scan in {"output", "both"}
            and self.pii.streaming_output == "bypass"
        ):
            issues.append(("WARN", "streaming responses bypass configured PII output scanning"))
        if self.features.moderation and self.moderation.failure_mode == "open":
            issues.append(("WARN", "moderation engine failures are configured to bypass policy"))
        if (
            self.features.moderation
            and self.moderation.scan in {"output", "both"}
            and self.moderation.streaming_output == "bypass"
        ):
            issues.append(("WARN", "streaming responses bypass configured output moderation"))
        if self.features.prompt_injection and self.prompt_injection.failure_mode == "open":
            issues.append(("WARN", "prompt-injection engine failures are configured to bypass policy"))
        if (
            self.features.prompt_injection
            and self.prompt_injection.action == "route"
            and not any(
                model.prompt_injection_safe
                for provider in self.providers if provider.enabled
                for model in provider.models
            )
        ):
            issues.append(("ERROR", "prompt-injection route policy has no eligible safe model"))
        if self.features.embeddings_api and not any(
            "embeddings" in model.capabilities
            for provider in self.providers if provider.enabled
            for model in provider.models
        ):
            issues.append(("ERROR", "embeddings API has no enabled embeddings-capable model"))
        if self.features.audio_api and not any(
            "audio_speech" in model.capabilities
            for provider in self.providers if provider.enabled
            for model in provider.models
        ):
            issues.append(("ERROR", "audio API has no enabled speech-capable model"))
        if self.features.images_api and not any(
            "image_generation" in model.capabilities
            for provider in self.providers if provider.enabled
            for model in provider.models
        ):
            issues.append(("ERROR", "images API has no enabled image-generation-capable model"))
        if self.features.cost_routing:
            priced_models = [
                model
                for provider in self.providers if provider.enabled
                for model in provider.models
                if model.pricing_version
                or model.input_cost_per_million > 0
                or model.output_cost_per_million > 0
            ]
            if not priced_models:
                severity = "ERROR" if self.routing.unknown_price_policy == "exclude" else "WARN"
                issues.append((severity, "cost routing has no models with known pricing"))
        if self.features.latency_routing and self.server.workers > 1:
            issues.append(("WARN", "latency routing observations are process-local across workers"))
        if self.features.routing_policy and any(
            policy.fallback == "default" and policy.residencies
            for policy in self.routing.policies
        ):
            issues.append(("WARN", "a residency routing policy permits default fallback"))
        if self.features.residency_enforcement:
            policy = next(item for item in self.routing.policies if item.name == self.residency.policy)
            if not any(
                provider.enabled and provider.api_key()
                and (not policy.providers or provider.name in policy.providers)
                and (not policy.regions or provider.region in policy.regions)
                and policy.residencies.issubset(provider.residencies)
                for provider in self.providers
            ):
                issues.append(("ERROR", "residency enforcement has no configured eligible provider"))
        if self.features.envelope_encryption:
            import base64
            import importlib.util
            import json

            raw_keys = os.getenv(self.encryption.keys_env)
            try:
                keyring = json.loads(raw_keys or "")
                decoded = {
                    str(version): base64.b64decode(value, validate=True)
                    for version, value in keyring.items()
                }
                if self.encryption.active_key_version not in decoded or any(
                    len(value) != 32 for value in decoded.values()
                ):
                    raise ValueError
            except Exception:
                issues.append(("ERROR", "envelope encryption keyring is missing or invalid"))
            if importlib.util.find_spec("cryptography") is None:
                issues.append(("ERROR", "envelope encryption requires the security package extra"))
        if self.features.oidc:
            import importlib.util

            if importlib.util.find_spec("jwt") is None:
                issues.append(("ERROR", "OIDC requires the oidc package extra"))
        if self.features.audit_ledger and self.audit.export_url and self.audit.export_url.startswith("http://"):
            issues.append(("WARN", "audit export URL is not encrypted"))
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


class RoutingPolicyConfig(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    operations: set[Literal["chat", "embeddings", "audio_speech", "image_generation"]] = Field(
        default_factory=lambda: {"chat", "embeddings", "audio_speech", "image_generation"}
    )
    models: set[str] = Field(default_factory=lambda: {"*"}, max_length=256)
    tiers: set[Literal["free", "paid"]] = Field(default_factory=lambda: {"free", "paid"})
    providers: set[str] = Field(default_factory=set, max_length=256)
    regions: set[str] = Field(default_factory=set, max_length=256)
    residencies: set[str] = Field(default_factory=set, max_length=32)
    min_quality: int = Field(default=0, ge=0, le=100)
    fallback: Literal["deny", "default"] = "deny"

    @model_validator(mode="after")
    def non_empty_matchers(self) -> "RoutingPolicyConfig":
        if not self.operations or not self.models or not self.tiers:
            raise ValueError("routing policy operations, models, and tiers must not be empty")
        for values in (self.models, self.providers, self.regions, self.residencies):
            if any(not value or len(value) > 128 for value in values):
                raise ValueError("routing policy values must be bounded non-empty strings")
        return self


class RoutingConfig(StrictModel):
    max_attempts: int = Field(default=0, ge=0)
    allow_provider_override: bool = False
    paid_fallback: bool = True
    unknown_price_policy: Literal["last", "exclude"] = "last"
    latency_ewma_alpha: float = Field(default=0.2, gt=0, le=1)
    latency_cold_start_ms: float = Field(default=1000, gt=0, le=3_600_000)
    latency_min_samples: int = Field(default=3, ge=1, le=10_000)
    latency_exploration_interval: int = Field(default=20, ge=0, le=1_000_000)
    allow_policy_override: bool = False
    policies: list[RoutingPolicyConfig] = Field(default_factory=list, max_length=256)
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
        names = [policy.name for policy in self.policies]
        if len(names) != len(set(names)):
            raise ValueError("routing policy names must be unique")
        return self


class ObservabilityConfig(StrictModel):
    include_upstream_errors: bool = True
    log_requests: bool = True
    log_provider_attempts: bool = True
    log_health_checks: bool = False
    include_exception_stacktraces: bool = False
    http_latency_buckets_seconds: list[float] = Field(
        default_factory=lambda: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
    )
    provider_latency_buckets_seconds: list[float] = Field(
        default_factory=lambda: [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120]
    )

    @model_validator(mode="after")
    def valid_latency_buckets(self) -> "ObservabilityConfig":
        for name in ("http_latency_buckets_seconds", "provider_latency_buckets_seconds"):
            values = getattr(self, name)
            if (
                not values
                or any(not math.isfinite(value) or value <= 0 for value in values)
                or values != sorted(set(values))
            ):
                raise ValueError(f"{name} must contain unique, increasing positive values")
        return self


class AccountingConfig(StrictModel):
    retention_days: int = Field(default=90, ge=30)


class PlanConfig(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pricing_version: str = Field(min_length=1, max_length=64)
    entitlements: set[str] = Field(default_factory=set, max_length=100)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_day: int | None = Field(default=None, gt=0)
    cost_per_30_days: float | None = Field(default=None, gt=0)
    included_credit_usd: float = Field(default=0, ge=0)
    budget_alert_percentages: list[int] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_plan(self) -> "PlanConfig":
        if any(not item or len(item) > 128 for item in self.entitlements):
            raise ValueError("plan entitlements must be bounded non-empty strings")
        if self.budget_alert_percentages != sorted(set(self.budget_alert_percentages)) or any(
            item <= 0 or item > 100 for item in self.budget_alert_percentages
        ):
            raise ValueError("budget alert percentages must be unique, increasing, and between 1 and 100")
        return self


class FinOpsConfig(StrictModel):
    plans: list[PlanConfig] = Field(default_factory=list, max_length=1000)
    export_url: str | None = None
    export_token_env: str | None = None
    export_sink_id: str = Field(default="default", min_length=1, max_length=128)
    export_batch_size: int = Field(default=1000, ge=1, le=10_000)
    export_timeout_seconds: float = Field(default=10, gt=0, le=60)

    @model_validator(mode="after")
    def unique_plans(self) -> "FinOpsConfig":
        ids = [item.id for item in self.plans]
        if len(ids) != len(set(ids)):
            raise ValueError("finops plan ids must be unique")
        if self.export_url and not self.export_url.startswith("https://"):
            raise ValueError("finops export_url must use HTTPS")
        return self

    def export_token(self) -> str | None:
        return os.getenv(self.export_token_env) if self.export_token_env else None


class JobsConfig(StrictModel):
    lease_seconds: int = Field(default=60, ge=5, le=3600)
    poll_interval_seconds: float = Field(default=0.25, ge=0.01, le=30)
    webhook_url: str | None = None
    webhook_secret_env: str = "LLM_GATEWAY_JOB_WEBHOOK_SECRET"
    webhook_timeout_seconds: float = Field(default=10, gt=0, le=60)
    webhook_attempts: int = Field(default=3, ge=1, le=10)

    def webhook_secret(self) -> str | None:
        return os.getenv(self.webhook_secret_env) or None

    @model_validator(mode="after")
    def valid_webhook(self) -> "JobsConfig":
        if self.webhook_url and not self.webhook_url.startswith("https://"):
            raise ValueError("job webhook_url must use HTTPS")
        return self


class BatchConfig(StrictModel):
    allow_payload_persistence: bool = False
    max_requests: int = Field(default=100, ge=1, le=10_000)
    max_payload_bytes: int = Field(default=10_485_760, ge=1024, le=67_108_864)


class CoordinationConfig(StrictModel):
    lease_seconds: float = Field(default=300, ge=5, le=3600)


class SemanticCacheConfig(StrictModel):
    embedding_version: str = Field(default="local-hash-v1", min_length=1, max_length=128)
    similarity_threshold: float = Field(default=0.92, gt=0, le=1)
    dimensions: int = Field(default=256, ge=32, le=4096)
    ttl_seconds: int = Field(default=3600, ge=1, le=604800)
    max_entries_per_tenant: int = Field(default=1000, ge=1, le=100_000)
    allow_sensitive_content: bool = False


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


class PIIConfig(StrictModel):
    action: Literal["detect", "redact", "block"] = "redact"
    scan: Literal["input", "output", "both"] = "input"
    detectors: set[Literal["email", "phone", "ipv4", "credit_card", "us_ssn"]] = Field(
        default_factory=lambda: {"email", "phone", "credit_card", "us_ssn"}
    )
    replacement: str = Field(default="[REDACTED:{type}]", min_length=1, max_length=128)
    max_scan_characters: int = Field(default=1_000_000, ge=1_024, le=10_000_000)
    failure_mode: Literal["open", "closed"] = "closed"
    streaming_output: Literal["block", "bypass"] = "block"

    @model_validator(mode="after")
    def valid_replacement(self) -> "PIIConfig":
        if "{" in self.replacement and "{type}" not in self.replacement:
            raise ValueError("pii replacement supports only the {type} placeholder")
        try:
            self.replacement.format(type="email")
        except (KeyError, ValueError) as exc:
            raise ValueError("pii replacement supports only the {type} placeholder") from exc
        if not self.detectors:
            raise ValueError("pii detectors must not be empty")
        return self


class ModerationConfig(StrictModel):
    action: Literal["monitor", "block"] = "block"
    scan: Literal["input", "output", "both"] = "input"
    categories: set[Literal["hate", "harassment", "self_harm", "sexual", "violence", "custom"]] = Field(
        default_factory=lambda: {"hate", "harassment", "self_harm", "sexual", "violence"}
    )
    custom_terms: list[str] = Field(default_factory=list, max_length=1_000)
    max_scan_characters: int = Field(default=1_000_000, ge=1_024, le=10_000_000)
    failure_mode: Literal["open", "closed"] = "closed"
    streaming_output: Literal["block", "bypass"] = "block"

    @model_validator(mode="after")
    def valid_policy(self) -> "ModerationConfig":
        normalized = [term.strip().casefold() for term in self.custom_terms]
        if any(not term or len(term) > 200 for term in normalized):
            raise ValueError("moderation custom terms must contain 1 to 200 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("moderation custom terms must be unique ignoring case")
        if "custom" in self.categories and not normalized:
            raise ValueError("moderation custom category requires custom_terms")
        if "custom" not in self.categories and normalized:
            raise ValueError("moderation custom_terms require the custom category")
        if not self.categories:
            raise ValueError("moderation categories must not be empty")
        self.custom_terms = normalized
        return self


class PromptInjectionConfig(StrictModel):
    action: Literal["score", "warn", "block", "route"] = "warn"
    threshold: float = Field(default=0.5, gt=0, le=1)
    signals: set[Literal[
        "instruction_override", "secret_exfiltration", "role_override",
        "tool_manipulation", "encoding_evasion",
    ]] = Field(default_factory=lambda: {
        "instruction_override", "secret_exfiltration", "role_override",
        "tool_manipulation", "encoding_evasion",
    })
    scan_roles: set[Literal["user", "tool"]] = Field(default_factory=lambda: {"user", "tool"})
    max_scan_characters: int = Field(default=1_000_000, ge=1_024, le=10_000_000)
    failure_mode: Literal["open", "closed"] = "closed"

    @model_validator(mode="after")
    def valid_policy(self) -> "PromptInjectionConfig":
        if not self.signals:
            raise ValueError("prompt-injection signals must not be empty")
        if not self.scan_roles:
            raise ValueError("prompt-injection scan_roles must not be empty")
        return self


class ResponsesConfig(StrictModel):
    max_input_items: int = Field(default=1_000, ge=1, le=10_000)
    max_tools: int = Field(default=128, ge=0, le=1_000)
    max_metadata_entries: int = Field(default=16, ge=0, le=100)


class EmbeddingsConfig(StrictModel):
    max_inputs: int = Field(default=2_048, ge=1, le=100_000)
    max_input_characters: int = Field(default=1_000_000, ge=1_024, le=10_000_000)
    max_token_ids: int = Field(default=1_000_000, ge=1_024, le=10_000_000)
    max_dimensions: int = Field(default=65_536, ge=1, le=1_000_000)
    allow_token_arrays: bool = True


class ModerationsAPIConfig(StrictModel):
    max_inputs: int = Field(default=256, ge=1, le=10_000)
    max_input_characters: int = Field(default=1_000_000, ge=1_024, le=10_000_000)
    default_model: str = Field(default="gateway-moderation-latest", min_length=1, max_length=128)
    accepted_models: set[str] = Field(default_factory=lambda: {
        "gateway-moderation-latest", "omni-moderation-latest", "text-moderation-latest",
    })

    @model_validator(mode="after")
    def valid_models(self) -> "ModerationsAPIConfig":
        if not self.accepted_models or any(not model or len(model) > 128 for model in self.accepted_models):
            raise ValueError("moderations accepted_models must contain bounded non-empty names")
        if self.default_model not in self.accepted_models:
            raise ValueError("moderations default_model must be listed in accepted_models")
        return self


class AudioConfig(StrictModel):
    max_input_characters: int = Field(default=4096, ge=1, le=100_000)
    response_formats: set[Literal["mp3", "opus", "aac", "flac", "wav", "pcm"]] = Field(
        default_factory=lambda: {"mp3", "opus", "aac", "flac", "wav", "pcm"}
    )
    max_response_bytes: int = Field(default=25_000_000, ge=1024, le=500_000_000)


class ImagesConfig(StrictModel):
    max_prompt_characters: int = Field(default=32_000, ge=1, le=1_000_000)
    max_images: int = Field(default=4, ge=1, le=100)
    max_response_bytes: int = Field(default=50_000_000, ge=1024, le=500_000_000)
    response_formats: set[Literal["url", "b64_json"]] = Field(
        default_factory=lambda: {"url", "b64_json"}
    )


class ModelDiscoveryConfig(StrictModel):
    allow_patterns: list[str] = Field(default_factory=lambda: ["*"])
    deny_patterns: list[str] = Field(default_factory=list)
    max_models_per_provider: int = Field(default=10_000, ge=1, le=100_000)
    max_concurrency: int = Field(default=8, ge=1, le=100)
    approval_mode: Literal["report_only"] = "report_only"

    @model_validator(mode="after")
    def valid_patterns(self) -> "ModelDiscoveryConfig":
        patterns = self.allow_patterns + self.deny_patterns
        if not self.allow_patterns or any(not item or len(item) > 256 for item in patterns):
            raise ValueError("model discovery patterns must be bounded non-empty strings")
        return self


class ConfigReloadConfig(StrictModel):
    max_generations: int = Field(default=10, ge=1, le=100)


class OIDCConfig(StrictModel):
    issuer: str | None = None
    audience: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwks_cache_seconds: int = Field(default=300, ge=30, le=86400)
    jwks_max_keys: int = Field(default=32, ge=1, le=128)
    clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    http_timeout_seconds: float = Field(default=5, gt=0, le=30)
    tenant_id_claim: str = "tenant_id"
    organization_id_claim: str = "organization_id"
    project_id_claim: str = "project_id"
    allow_insecure_http: bool = False
    max_token_bytes: int = Field(default=16_384, ge=1024, le=131_072)

    @model_validator(mode="after")
    def validate_security(self) -> "OIDCConfig":
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if not self.algorithms or not set(self.algorithms) <= allowed:
            raise ValueError("OIDC algorithms must be explicit asymmetric signing algorithms")
        if self.issuer and not self.issuer.startswith("https://") and not self.allow_insecure_http:
            raise ValueError("OIDC issuer must use HTTPS")
        claims = (self.tenant_id_claim, self.organization_id_claim, self.project_id_claim)
        if any(not value or len(value) > 128 for value in claims):
            raise ValueError("OIDC claim mappings must be bounded non-empty names")
        return self


class AuditConfig(StrictModel):
    export_url: str | None = None
    export_token_env: str | None = None
    export_batch_size: int = Field(default=500, ge=1, le=10_000)
    export_timeout_seconds: float = Field(default=10, gt=0, le=60)
    read_max_events: int = Field(default=1000, ge=1, le=10_000)


class EncryptionConfig(StrictModel):
    keys_env: str = "LLM_GATEWAY_ENCRYPTION_KEYS"
    active_key_version: str = Field(default="v1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    rotation_batch_size: int = Field(default=100, ge=1, le=10_000)
    allow_plaintext_migration: bool = False


class ResidencyConfig(StrictModel):
    policy: str = Field(default="residency", min_length=1, max_length=64)


class FeatureConfig(StrictModel):
    authentication: bool | None = None
    admin_api: bool | None = None
    managed_keys: bool | None = None
    identity_hierarchy: bool | None = None
    rbac: bool | None = None
    oidc: bool | None = None
    audit_ledger: bool | None = None
    envelope_encryption: bool | None = None
    residency_enforcement: bool | None = None
    finops_plans: bool | None = None
    finops_export: bool | None = None
    async_jobs: bool | None = None
    batch_api: bool | None = None
    distributed_resilience: bool | None = None
    semantic_cache: bool | None = None
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
    pii_detection: bool | None = None
    moderation: bool | None = None
    prompt_injection: bool | None = None
    responses_api: bool | None = None
    embeddings_api: bool | None = None
    moderations_api: bool | None = None
    audio_api: bool | None = None
    images_api: bool | None = None
    model_discovery: bool | None = None
    cost_routing: bool | None = None
    latency_routing: bool | None = None
    routing_policy: bool | None = None
    config_reload: bool | None = None


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
        for credential in provider.get("credentials", []):
            if credential.get("api_key_file"):
                credential["api_key_file"] = str(
                    (config_path.parent / credential["api_key_file"]).resolve()
                )
    return GatewayConfig.model_validate(data)


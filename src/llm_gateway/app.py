from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import secrets
import hashlib
import logging
import os
import time
from typing import Any
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Limit, load_config
from .metrics import Metrics, NullMetrics
from .identity import AuthContext, IdentityStore
from .logging_config import begin_request, bind_tenant, end_request
from .quota import NullQuotaStore, QuotaStore
from .router import GatewayRouter, NoModelAvailable, QuotaExceeded

logger = logging.getLogger("llm_gateway")


def _reload_immutable_state(config) -> dict[str, Any]:
    state = config.model_dump(mode="json")
    state.pop("routing", None)
    state.pop("providers", None)
    return state


def _provider_client(config) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=config.routing.http_max_connections,
            max_keepalive_connections=config.routing.http_max_keepalive_connections,
            keepalive_expiry=config.routing.http_keepalive_expiry_seconds,
        ),
    )


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_day: int | None = Field(default=None, gt=0)
    cost_per_30_days: float | None = Field(default=None, gt=0)
    organization_id: str | None = Field(default=None, min_length=1, max_length=64)
    plan_id: str | None = Field(default=None, min_length=1, max_length=64)


class NamedIdentityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


class PrincipalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(user|service_account)$")
    subject: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=120)
    project_id: str | None = Field(default=None, min_length=1, max_length=64)


class NamedIdentityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None

    def changes(self) -> dict[str, object]:
        values = self.model_dump(exclude_none=True)
        if not values:
            raise HTTPException(400, "at least one hierarchy change is required")
        return values


class PrincipalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None

    def changes(self) -> dict[str, object]:
        values = self.model_dump(exclude_none=True)
        if not values:
            raise HTTPException(400, "at least one principal change is required")
        return values


class ModelDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: list[str] | None = Field(default=None, max_length=100)


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, gt=0, le=3650)
    principal_id: str | None = Field(default=None, min_length=1, max_length=64)
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    permissions: list[str] | None = Field(default=None, max_length=100)


class RoleBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str = Field(min_length=1, max_length=64)
    role_name: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(default=None, min_length=1, max_length=64)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_day: int | None = Field(default=None, gt=0)
    cost_per_30_days: float | None = Field(default=None, gt=0)
    organization_id: str | None = Field(default=None, min_length=1, max_length=64)
    plan_id: str | None = Field(default=None, min_length=1, max_length=64)


class CreditGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_usd: float = Field(gt=0, le=1_000_000_000)
    source: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, gt=0, le=3650)


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    custom_id: str = Field(min_length=1, max_length=128)
    body: dict[str, Any]


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[BatchItem] = Field(min_length=1)


class BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            content_length = int(headers.get(b"content-length", b"0") or 0)
        except ValueError:
            content_length = 0
        if content_length > self.max_bytes:
            return await JSONResponse(
                status_code=413,
                content={"error": {"message": "request body too large", "type": "invalid_request_error", "code": "body_too_large"}},
            )(scope, receive, send)
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > self.max_bytes:
                raise BodyTooLarge
            return message

        try:
            return await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            return await JSONResponse(
                status_code=413,
                content={"error": {"message": "request body too large", "type": "invalid_request_error", "code": "body_too_large"}},
            )(scope, receive, send)


def request_with_json(request: Request, payload: dict[str, Any]) -> Request:
    """Create an internal request that preserves auth/context but substitutes a JSON body."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    scope = dict(request.scope)
    headers = [
        (name, value) for name, value in scope.get("headers", [])
        if name.lower() not in {b"content-length", b"content-type"}
    ]
    headers.extend([
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ])
    scope["headers"] = headers
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    config_path = str(config_path)
    reload_clients: list[httpx.AsyncClient] = []
    reload_lock = asyncio.Lock()
    reload_generation = 0
    envelope_cipher = None
    if config.features.envelope_encryption:
        from .encryption import EnvelopeCipher

        envelope_cipher = EnvelopeCipher.from_environment(
            config.encryption.keys_env, config.encryption.active_key_version
        )
    quota_enabled = bool(
        config.features.provider_quotas
        or config.features.tenant_quotas
        or config.features.usage_accounting
    )
    identity_enabled = bool(config.features.managed_keys or config.features.identity_hierarchy)
    plans = {item.id: item for item in config.finops.plans} if config.features.finops_plans else {}
    shared_store = None
    if config.database.startswith(("postgresql://", "postgres://")) and (quota_enabled or identity_enabled):
        from .postgres import PostgresStore
        shared_store = PostgresStore(
            config.database, config.auth.key_cache_seconds,
            config.auth.key_cache_max_entries,
            identity_enabled=identity_enabled,
            provider_quotas=bool(config.features.provider_quotas),
            tenant_quotas=bool(config.features.tenant_quotas),
            accounting=bool(config.features.usage_accounting),
            cost_tracking=bool(config.features.cost_tracking),
            rbac_enabled=bool(config.features.rbac),
            plans=plans,
        )
        quota = shared_store if quota_enabled else NullQuotaStore()
        identities = shared_store if identity_enabled else None
    else:
        quota = (
            QuotaStore(
                config.database,
                provider_quotas=bool(config.features.provider_quotas),
                tenant_quotas=bool(config.features.tenant_quotas),
                accounting=bool(config.features.usage_accounting),
                cost_tracking=bool(config.features.cost_tracking),
            ) if quota_enabled else NullQuotaStore()
        )
        identities = (
            IdentityStore(
                config.database, config.auth.key_cache_seconds,
                config.auth.key_cache_max_entries,
                rbac_enabled=bool(config.features.rbac),
                plans=plans,
            ) if identity_enabled else None
        )
    metrics = (
        Metrics(
            tuple(config.observability.http_latency_buckets_seconds),
            tuple(config.observability.provider_latency_buckets_seconds),
        )
        if config.features.metrics else NullMetrics()
    )
    idempotency_store = None
    if config.features.idempotency:
        from .idempotency import PostgresIdempotencyStore, SQLiteIdempotencyStore

        store_type = (
            PostgresIdempotencyStore
            if config.database.startswith(("postgresql://", "postgres://"))
            else SQLiteIdempotencyStore
        )
        idempotency_store = store_type(
            config.database, config.idempotency.ttl_seconds,
            config.idempotency.lease_seconds, cipher=envelope_cipher,
            allow_plaintext_migration=config.encryption.allow_plaintext_migration,
        )
    cache_store = None
    cache_available = False
    cache_routing_signature = ""
    if config.features.exact_cache:
        from .cache import MemoryCacheStore, PostgresCacheStore, RedisCacheStore, SQLiteCacheStore

        if config.cache.backend == "memory":
            cache_store = MemoryCacheStore(config.cache.ttl_seconds, config.cache.max_entries)
        elif config.cache.backend == "redis":
            cache_store = RedisCacheStore(
                config.cache.redis_url() or "", config.cache.ttl_seconds, config.cache.redis_prefix,
                cipher=envelope_cipher,
                allow_plaintext_migration=config.encryption.allow_plaintext_migration,
            )
        elif config.database.startswith(("postgresql://", "postgres://")):
            cache_store = PostgresCacheStore(
                config.database, config.cache.ttl_seconds, config.cache.max_entries,
                cipher=envelope_cipher,
                allow_plaintext_migration=config.encryption.allow_plaintext_migration,
            )
        else:
            cache_store = SQLiteCacheStore(
                config.database, config.cache.ttl_seconds, config.cache.max_entries,
                cipher=envelope_cipher,
                allow_plaintext_migration=config.encryption.allow_plaintext_migration,
            )
        routing_material = {
            "paid_fallback": config.routing.paid_fallback,
            "routing_policies": (
                [policy.model_dump(mode="json") for policy in config.routing.policies]
                if config.features.routing_policy else None
            ),
            "pii_policy": (
                {
                    "action": config.pii.action,
                    "scan": config.pii.scan,
                    "detectors": sorted(config.pii.detectors),
                    "replacement": config.pii.replacement,
                    "streaming_output": config.pii.streaming_output,
                }
                if config.features.pii_detection else None
            ),
            "moderation_policy": (
                {
                    "action": config.moderation.action,
                    "scan": config.moderation.scan,
                    "categories": sorted(config.moderation.categories),
                    "custom_terms": config.moderation.custom_terms,
                    "streaming_output": config.moderation.streaming_output,
                }
                if config.features.moderation else None
            ),
            "prompt_injection_policy": (
                {
                    "action": config.prompt_injection.action,
                    "threshold": config.prompt_injection.threshold,
                    "signals": sorted(config.prompt_injection.signals),
                    "scan_roles": sorted(config.prompt_injection.scan_roles),
                }
                if config.features.prompt_injection else None
            ),
            "providers": [
                {
                    "name": provider.name, "enabled": provider.enabled, "tier": provider.tier,
                    "region": provider.region, "residencies": sorted(provider.residencies),
                    "base_url": provider.base_url, "path": provider.chat_completions_path,
                    "defaults": provider.request_defaults,
                    "models": [
                        {
                            "name": model.name, "upstream": model.upstream_model,
                            "priority": model.priority, "capabilities": sorted(model.capabilities),
                            "quality": model.quality,
                            "prompt_injection_safe": model.prompt_injection_safe,
                            "overrides": model.request_overrides,
                        }
                        for model in provider.models
                    ],
                }
                for provider in config.providers
            ],
        }
        cache_routing_signature = hashlib.sha256(
            json.dumps(routing_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        cache_available = True
    semantic_cache_store = None
    if config.features.semantic_cache:
        from .semantic_cache import MemorySemanticCache

        semantic_cache_store = MemorySemanticCache(config.semantic_cache)
    telemetry = None
    if config.features.tracing:
        try:
            from .telemetry import Telemetry

            telemetry = Telemetry(config.telemetry)
        except Exception as exc:
            if config.telemetry.failure_mode == "closed":
                raise
            logger.error(
                "OpenTelemetry initialization failed; continuing without tracing error_type=%s",
                type(exc).__name__,
            )
    pii_processor = None
    if config.features.pii_detection:
        from .pii import PIIProcessor

        pii_processor = PIIProcessor(config.pii)
    moderation_processor = None
    if config.features.moderation:
        from .moderation import ModerationProcessor

        moderation_processor = ModerationProcessor(config.moderation)
    moderation_api_processor = None
    if config.features.moderations_api:
        from .moderation import ModerationProcessor

        moderation_api_processor = ModerationProcessor(
            config.moderation.model_copy(update={"action": "monitor"})
        )
    injection_processor = None
    if config.features.prompt_injection:
        from .prompt_injection import PromptInjectionProcessor

        injection_processor = PromptInjectionProcessor(config.prompt_injection)
    oidc_validator = None
    if config.features.oidc:
        from .oidc import OIDCValidator

        oidc_validator = OIDCValidator(config.oidc)
    audit_store = None
    if config.features.audit_ledger:
        from .audit import PostgresAuditStore, SQLiteAuditStore

        audit_store = (
            PostgresAuditStore(config.database)
            if config.database.startswith(("postgresql://", "postgres://"))
            else SQLiteAuditStore(config.database)
        )
    job_store = None
    if config.features.async_jobs:
        from .jobs import PostgresJobStore, SQLiteJobStore

        job_store = (
            PostgresJobStore(config.database)
            if config.database.startswith(("postgresql://", "postgres://"))
            else SQLiteJobStore(config.database)
        )
    coordinator = None
    if config.features.distributed_resilience:
        from .coordination import PostgresCoordinator

        coordinator = PostgresCoordinator(config.database, config.coordination.lease_seconds)

    async def run_worker(worker, name: str) -> None:
        while True:
            try:
                worked = await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("%s worker iteration failed error_type=%s", name, type(exc).__name__)
                worked = False
            if not worked:
                await asyncio.sleep(config.jobs.poll_interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal cache_available
        await quota.initialize()
        if identities is not None and identities is not quota:
            await identities.initialize()
        if idempotency_store is not None:
            await idempotency_store.initialize()
        if cache_store is not None:
            try:
                await cache_store.initialize()
            except Exception as exc:
                if config.cache.failure_mode == "closed":
                    raise
                cache_available = False
                logger.error(
                    "Exact cache initialization failed; continuing with cache disabled error_type=%s",
                    type(exc).__name__,
                )
        if audit_store is not None:
            await audit_store.initialize()
        if job_store is not None:
            await job_store.initialize()
        if coordinator is not None:
            await coordinator.initialize()
        removed = await quota.cleanup(
            time.time() - config.accounting.retention_days * 86400
        )
        if removed:
            logger.info("Removed %s expired accounting records", removed)
        async with _provider_client(config) as client:
            app.state.router = GatewayRouter(config, quota, client, metrics, telemetry, coordinator)
            app.state.active_config = config
            worker_tasks = []
            if config.features.batch_api:
                from .jobs import JobWorker, SignedWebhookNotifier, WebhookDeliveryWorker

                notifier = None
                if config.jobs.webhook_url:
                    notifier = SignedWebhookNotifier(
                        config.jobs.webhook_url, config.jobs.webhook_secret() or "", client,
                        config.jobs.webhook_attempts, config.jobs.webhook_timeout_seconds,
                    )

                async def handle_chat_batch(job: dict[str, object]) -> dict[str, object]:
                    payload = job["payload"]
                    limits = tuple(
                        Limit.model_validate(item) for item in payload.get("tenant_limits", [])
                    )
                    results = []
                    for item in payload["requests"]:
                        try:
                            body, _provider, _model = await app.state.router.complete_for(
                                item["body"], str(job["tenant_id"]), limits,
                            )
                            results.append({"custom_id": item["custom_id"], "response": body})
                        except QuotaExceeded:
                            results.append({
                                "custom_id": item["custom_id"],
                                "error": {"code": "quota_exceeded", "message": "quota exceeded"},
                            })
                        except NoModelAvailable:
                            results.append({
                                "custom_id": item["custom_id"],
                                "error": {"code": "no_model_available", "message": "no model available"},
                            })
                    return {"object": "batch_result", "results": results}

                job_worker = JobWorker(
                    job_store, {"chat_batch": handle_chat_batch},
                    lease_seconds=config.jobs.lease_seconds, notifier=notifier,
                    restrict_to_handlers=True,
                )
                worker_tasks.append(asyncio.create_task(run_worker(job_worker, "batch")))
                if notifier is not None:
                    delivery_worker = WebhookDeliveryWorker(
                        job_store, notifier, lease_seconds=config.jobs.lease_seconds,
                    )
                    worker_tasks.append(
                        asyncio.create_task(run_worker(delivery_worker, "job webhook"))
                    )
            try:
                yield
            finally:
                for task in worker_tasks:
                    task.cancel()
                for task in worker_tasks:
                    with suppress(asyncio.CancelledError):
                        await task
        for reload_client in reload_clients:
            await reload_client.aclose()
        close = getattr(shared_store or quota, "close", None)
        if close:
            await close()
        if idempotency_store is not None:
            await idempotency_store.close()
        if cache_store is not None:
            await cache_store.close()
        if semantic_cache_store is not None:
            await semantic_cache_store.close()
        if job_store is not None:
            await job_store.close()
        if coordinator is not None:
            await coordinator.close()
        if oidc_validator is not None:
            await oidc_validator.close()
        if audit_store is not None:
            close_audit = getattr(audit_store, "close", None)
            if close_audit:
                await close_audit()
        if telemetry is not None:
            try:
                await asyncio.to_thread(telemetry.shutdown)
            except Exception as exc:
                if config.telemetry.failure_mode == "closed":
                    raise
                logger.error("OpenTelemetry shutdown failed error_type=%s", type(exc).__name__)

    app = FastAPI(
        title="LLM Gateway", version=__version__, lifespan=lifespan,
        docs_url="/docs" if config.server.docs_enabled else None,
        redoc_url="/redoc" if config.server.docs_enabled else None,
    )
    app.add_middleware(BodyLimitMiddleware, max_bytes=config.server.max_request_body_bytes)
    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=config.server.cors_origins,
            allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
        )

    @app.exception_handler(StarletteHTTPException)
    async def openai_http_error(_request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code, headers=exc.headers,
            content={"error": {"message": message, "type": "gateway_error", "code": exc.status_code}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"message": "request validation failed", "type": "invalid_request_error", "code": "validation_error", "details": exc.errors()}},
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        logger.exception(
            "Unhandled gateway error request_id=%s path=%s",
            getattr(request.state, "request_id", "unknown"), request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "internal gateway error", "type": "gateway_error", "code": 500}},
        )

    def bearer(request: Request) -> str:
        value = request.headers.get("authorization", "")
        return value[7:] if value.lower().startswith("bearer ") else ""

    def enforce_plan_entitlement(request: Request, context: AuthContext) -> None:
        if not config.features.finops_plans or context.plan_id is None:
            return
        entitlement = {
            "/v1/chat/completions": "chat",
            "/v1/responses": "responses",
            "/v1/embeddings": "embeddings",
            "/v1/moderations": "moderations",
            "/v1/audio/speech": "audio",
            "/v1/images/generations": "images",
            "/v1/batches": "chat",
        }.get(request.url.path)
        if entitlement and entitlement not in context.entitlements:
            raise HTTPException(403, f"plan does not entitle {entitlement}")

    async def require_api(request: Request) -> AuthContext:
        existing = getattr(request.state, "auth_context", None)
        if existing is not None:
            if config.features.rbac and "gateway.invoke" not in existing.permissions:
                await metrics.security("rbac", "denied")
                raise HTTPException(403, "permission denied")
            return existing
        if not config.features.authentication:
            context = AuthContext("anonymous", "anonymous")
            if config.features.rbac:
                await metrics.security("rbac", "denied")
                raise HTTPException(403, "permission denied")
            request.state.auth_context = context
            request.state.tenant_id = context.tenant_id
            if config.features.structured_logging:
                bind_tenant(context.tenant_id)
            if telemetry is not None:
                telemetry.set_tenant(context.tenant_id)
            enforce_plan_entitlement(request, context)
            return context
        token = bearer(request)
        for key in config.auth.api_keys():
            if secrets.compare_digest(token, key):
                identity = hashlib.sha256(key.encode()).hexdigest()[:16]
                context = AuthContext(f"bootstrap-{identity}", f"bootstrap-{identity}")
                if config.features.rbac:
                    await metrics.security("rbac", "denied")
                    raise HTTPException(403, "permission denied")
                request.state.auth_context = context
                request.state.tenant_id = context.tenant_id
                if config.features.structured_logging:
                    bind_tenant(context.tenant_id)
                if telemetry is not None:
                    telemetry.set_tenant(context.tenant_id)
                enforce_plan_entitlement(request, context)
                return context
        if config.features.managed_keys and identities is not None and token:
            context = await identities.authenticate(token)
            if context:
                if config.features.rbac and "gateway.invoke" not in context.permissions:
                    await metrics.security("rbac", "denied")
                    raise HTTPException(403, "permission denied")
                if config.features.rbac:
                    await metrics.security("rbac", "success")
                request.state.auth_context = context
                request.state.tenant_id = context.tenant_id
                if config.features.structured_logging:
                    bind_tenant(context.tenant_id)
                if telemetry is not None:
                    telemetry.set_tenant(context.tenant_id)
                enforce_plan_entitlement(request, context)
                return context
        if config.features.oidc and oidc_validator is not None and identities is not None and token:
            try:
                identity = await oidc_validator.validate(token)
                context = await identities.authenticate_oidc(
                    identity.issuer, identity.subject, identity.tenant_id,
                    identity.organization_id, identity.project_id,
                )
            except Exception:
                context = None
                await metrics.security("oidc", "denied")
            if context and "gateway.invoke" in context.permissions:
                await metrics.security("oidc", "success")
                request.state.auth_context = context
                request.state.tenant_id = context.tenant_id
                if config.features.structured_logging:
                    bind_tenant(context.tenant_id)
                if telemetry is not None:
                    telemetry.set_tenant(context.tenant_id)
                enforce_plan_entitlement(request, context)
                return context
        raise HTTPException(401, "invalid gateway API key", headers={"WWW-Authenticate": "Bearer"})

    def require_admin(request: Request) -> None:
        if not config.features.authentication:
            return
        expected = config.auth.admin_key()
        if expected and secrets.compare_digest(bearer(request), expected):
            return
        context = getattr(request.state, "admin_auth_context", None)
        if not config.features.rbac or context is None:
            raise HTTPException(403, "administrator credentials required")
        path, method = request.url.path, request.method
        if path.startswith(("/admin/audit", "/admin/usage", "/admin/encryption",
                            "/admin/providers", "/admin/config", "/admin/model-discovery")):
            raise HTTPException(403, "root administrator credentials required")
        elif "/role-bindings" in path:
            permission = "roles.read" if method == "GET" else "roles.manage"
        elif "/principals" in path:
            permission = "principals.read" if method == "GET" else "principals.manage"
        elif "/projects" in path:
            permission = "projects.read" if method == "GET" else "projects.manage"
        elif path.startswith("/admin/organizations"):
            if path == "/admin/organizations" and method != "GET":
                raise HTTPException(403, "root administrator credentials required")
            permission = "organizations.read" if method == "GET" else "organizations.manage"
        elif "/keys" in path:
            permission = "keys.read" if method == "GET" else ("keys.revoke" if method == "DELETE" else "keys.issue")
        else:
            raise HTTPException(403, "root administrator credentials required")
        if permission not in context.permissions:
            raise HTTPException(403, "permission denied")
        organization_id = request.path_params.get("organization_id")
        if organization_id and organization_id != context.organization_id:
            raise HTTPException(403, "organization scope mismatch")

    async def record_admin_event(
        request: Request, event_type: str, tenant_id: str = "system",
        organization_id: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> None:
        if audit_store is None:
            return
        context = getattr(request.state, "admin_auth_context", None)
        if context is not None:
            actor_id = f"principal:{context.principal_id}"
        else:
            expected = config.auth.admin_key() or "local-admin"
            actor_id = f"admin:{hashlib.sha256(expected.encode()).hexdigest()[:16]}"
        await audit_store.append(
            tenant_id, actor_id, event_type, "success", organization_id,
            {"request_id": request.state.request_id, **(metadata or {})},
        )
        await metrics.security("audit_append", "success")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        instrumented = bool(config.features.metrics or config.features.structured_logging)
        started = time.monotonic() if instrumented else 0
        supplied_id = request.headers.get("x-request-id", "")
        request_id = supplied_id if supplied_id and len(supplied_id) <= 128 and supplied_id.isprintable() else str(uuid.uuid4())
        request.state.request_id = request_id
        context_tokens = begin_request(request_id) if config.features.structured_logging else None
        trace_handle = (
            telemetry.start_request(request.headers, request.method, request.url.path, request_id)
            if telemetry is not None else None
        )
        trace_status, trace_error = 500, None
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                trace_error = type(exc).__name__
                elapsed = (time.monotonic() - started) * 1000
                if config.features.metrics:
                    await metrics.request(request.url.path, 500, elapsed)
                if config.features.structured_logging and config.observability.log_requests:
                    logger.error(
                        "HTTP request failed",
                        extra={"event_name": "http.request", "http_method": request.method,
                               "http_path": request.url.path, "http_status_code": 500,
                               "duration_ms": round(elapsed, 3),
                               "tenant_id": getattr(request.state, "tenant_id", None)},
                    )
                raise
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            elapsed = (time.monotonic() - started) * 1000 if instrumented else 0
            trace_status = response.status_code
            if config.features.metrics:
                await metrics.request(request.url.path, response.status_code, elapsed)
            should_log = (
                config.features.structured_logging and config.observability.log_requests
                and (config.observability.log_health_checks or request.url.path not in {"/health", "/ready"})
            )
            if should_log:
                logger.info(
                    "HTTP request completed",
                    extra={"event_name": "http.request", "http_method": request.method,
                           "http_path": request.url.path, "http_status_code": response.status_code,
                           "duration_ms": round(elapsed, 3),
                           "tenant_id": getattr(request.state, "tenant_id", None)},
                )
            return response
        finally:
            if telemetry is not None:
                telemetry.finish_request(trace_handle, trace_status, trace_error)
            if context_tokens is not None:
                end_request(context_tokens)

    @app.middleware("http")
    async def delegated_admin_auth(request: Request, call_next):
        if (
            config.features.rbac and identities is not None
            and request.url.path.startswith("/admin/")
        ):
            token = bearer(request)
            expected = config.auth.admin_key()
            if token and not (expected and secrets.compare_digest(token, expected)):
                context = await identities.authenticate(token)
                if context is not None:
                    request.state.admin_auth_context = context
        response = await call_next(request)
        if audit_store is not None and request.url.path.startswith("/admin/") and response.status_code == 403:
            context = getattr(request.state, "admin_auth_context", None)
            await audit_store.append(
                getattr(context, "tenant_id", "system"),
                f"principal:{context.principal_id}" if context else "unauthenticated",
                "admin.denied", "denied", getattr(context, "organization_id", None),
                {"http_method": request.method},
            )
            await metrics.security("admin_authorization", "denied")
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "name": "LLM Gateway", "version": __version__, "status": "ok",
            "endpoints": {
                "health": "/health", "ready": "/ready", "models": "/v1/models",
                **({"responses": "/v1/responses"} if config.features.responses_api else {}),
                **({"embeddings": "/v1/embeddings"} if config.features.embeddings_api else {}),
                **({"moderations": "/v1/moderations"} if config.features.moderations_api else {}),
                **({"audio_speech": "/v1/audio/speech"} if config.features.audio_api else {}),
                **({"image_generations": "/v1/images/generations"} if config.features.images_api else {}),
                "docs": "/docs" if config.server.docs_enabled else None,
            },
        }

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, object]:
        configured = [
            item for item in request.app.state.router.status()
            if item["enabled"] and item["configured"]
        ]
        if not configured:
            raise HTTPException(503, "no enabled provider has valid credentials")
        errors = [message for severity, message in config.diagnostics() if severity == "ERROR"]
        if errors:
            raise HTTPException(503, "; ".join(errors))
        return {"status": "ready", "configured_providers": len(configured)}

    @app.get("/v1/models")
    async def models(request: Request) -> dict[str, Any]:
        await require_api(request)
        names = sorted({
            model.name for provider in config.providers
            if provider.enabled and provider.api_key() for model in provider.models
        })
        return {"object": "list", "data": [{"id": name, "object": "model"} for name in names]}

    @app.get("/admin/usage")
    async def usage(request: Request) -> list[dict[str, object]]:
        require_admin(request)
        return await quota.snapshot()

    @app.get("/admin/usage/tenants")
    async def tenant_usage(request: Request) -> list[dict[str, object]]:
        require_admin(request)
        return await quota.consumer_snapshot()

    @app.get("/admin/usage/export")
    async def usage_export(
        request: Request, after: float = 0, after_id: str = "", limit: int = 1000,
    ) -> dict[str, object]:
        require_admin(request)
        if after < 0 or limit < 1 or limit > 10_000 or len(after_id) > 255:
            raise HTTPException(400, "invalid export cursor or limit")
        events = await quota.usage_export(after, after_id, limit)
        encoded = json.dumps(events, sort_keys=True, separators=(",", ":"), default=str).encode()
        last = events[-1] if events else None
        return {
            "schema_version": 1, "events": events,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "next": {
                "after": last["created_at"] if last else after,
                "after_id": last["request_id"] if last else after_id,
            },
        }

    @app.get("/admin/usage/reconcile")
    async def usage_reconcile(request: Request) -> dict[str, object]:
        require_admin(request)
        events = await quota.usage_export(0, "", 10_000)
        return {
            "schema_version": 1,
            "complete": len(events) < 10_000,
            "event_count": len(events),
            "requests": sum(int(item["requests"]) for item in events),
            "tokens": sum(int(item["tokens"]) for item in events),
            "cost_usd": round(sum(float(item["cost_usd"]) for item in events), 8),
        }

    @app.post("/admin/usage/export-deliver")
    async def deliver_usage_export(request: Request) -> dict[str, object]:
        require_admin(request)
        after, after_id = await quota.export_checkpoint(config.finops.export_sink_id)
        events = await quota.usage_export(after, after_id, config.finops.export_batch_size)
        if not events:
            return {"exported": 0, "checkpoint": {"after": after, "after_id": after_id}}
        encoded_events = json.dumps(
            events, sort_keys=True, separators=(",", ":"), default=str,
        ).encode()
        page_sha256 = hashlib.sha256(encoded_events).hexdigest()
        payload = json.dumps({
            "schema_version": 1, "page_sha256": page_sha256, "events": events,
        }, sort_keys=True, separators=(",", ":"), default=str).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": page_sha256}
        if token := config.finops.export_token():
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(follow_redirects=False) as export_client:
                response = await export_client.post(
                    config.finops.export_url, content=payload, headers=headers,
                    timeout=config.finops.export_timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(502, "usage export delivery failed") from exc
        if not 200 <= response.status_code < 300:
            raise HTTPException(502, "usage export sink rejected page")
        last = events[-1]
        await quota.save_export_checkpoint(
            config.finops.export_sink_id, float(last["created_at"]),
            str(last["request_id"]), page_sha256,
        )
        return {
            "exported": len(events), "page_sha256": page_sha256,
            "checkpoint": {"after": last["created_at"], "after_id": last["request_id"]},
        }

    @app.post("/admin/tenants", status_code=201)
    async def create_tenant(request: Request, tenant: TenantCreate) -> dict[str, object]:
        require_admin(request)
        if tenant.plan_id is not None and tenant.plan_id not in plans:
            raise HTTPException(400, "unknown plan_id")
        try:
            created = await identities.create_tenant(**tenant.model_dump())
            await record_admin_event(
                request, "tenant.created", str(created["id"]),
                created.get("organization_id") if isinstance(created.get("organization_id"), str) else None,
            )
            return created
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate key" in str(exc).lower():
                raise HTTPException(409, "tenant name already exists") from exc
            raise

    @app.get("/admin/tenants")
    async def list_tenants(request: Request) -> list[dict[str, object]]:
        require_admin(request)
        return await identities.list_tenants()

    @app.get("/admin/plans")
    async def list_plans(request: Request) -> list[dict[str, object]]:
        require_admin(request)
        return [item.model_dump(mode="json") for item in config.finops.plans]

    @app.get("/v1/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> dict[str, object]:
        context = await require_api(request)
        job = await job_store.get(context.tenant_id, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.delete("/v1/jobs/{job_id}")
    async def cancel_job(request: Request, job_id: str) -> dict[str, object]:
        context = await require_api(request)
        job = await job_store.cancel(context.tenant_id, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.post("/v1/batches", status_code=202)
    async def create_batch(request: Request, batch: BatchCreate) -> dict[str, object]:
        context = await require_api(request)
        if len(batch.requests) > config.batch.max_requests:
            raise HTTPException(400, "batch exceeds configured request limit")
        custom_ids = [item.custom_id for item in batch.requests]
        if len(custom_ids) != len(set(custom_ids)):
            raise HTTPException(400, "batch custom_id values must be unique")
        items = []
        for item in batch.requests:
            if item.body.get("stream"):
                raise HTTPException(400, "streaming requests are not supported in batches")
            if not isinstance(item.body.get("messages"), list):
                raise HTTPException(400, "each batch request requires a messages array")
            items.append(item.model_dump(mode="json"))
        encoded = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > config.batch.max_payload_bytes:
            raise HTTPException(413, "batch payload exceeds configured byte limit")
        payload = {
            "requests": items,
            "tenant_limits": [item.model_dump(mode="json") for item in context.limits],
        }
        job = await job_store.create(context.tenant_id, "chat_batch", payload)
        return {**job, "object": "batch"}

    @app.get("/v1/batches/{job_id}")
    async def get_batch(request: Request, job_id: str) -> dict[str, object]:
        context = await require_api(request)
        job = await job_store.get(context.tenant_id, job_id)
        if job is None or job["kind"] != "chat_batch":
            raise HTTPException(404, "batch not found")
        return {**job, "object": "batch"}

    @app.delete("/v1/batches/{job_id}")
    async def cancel_batch(request: Request, job_id: str) -> dict[str, object]:
        context = await require_api(request)
        existing = await job_store.get(context.tenant_id, job_id)
        if existing is None or existing["kind"] != "chat_batch":
            raise HTTPException(404, "batch not found")
        job = await job_store.cancel(context.tenant_id, job_id)
        return {**job, "object": "batch"}

    @app.get("/admin/tenants/{tenant_id}/budget")
    async def tenant_budget(request: Request, tenant_id: str) -> dict[str, object]:
        require_admin(request)
        tenant = next((item for item in await identities.list_tenants() if item["id"] == tenant_id), None)
        if tenant is None:
            raise HTTPException(404, "tenant not found")
        plan = plans.get(tenant.get("plan_id"))
        if plan is None:
            raise HTTPException(409, "tenant has no active configured plan")
        usage = next(
            (item for item in await quota.consumer_snapshot() if item["tenant_id"] == tenant_id),
            {"requests": 0, "tokens": 0, "cost_usd": 0},
        )
        cost = float(usage["cost_usd"] or 0)
        now = time.time()
        credit_grants = await identities.list_credits(tenant_id)
        active_grants = [
            item for item in credit_grants
            if item["expires_at"] is None or float(item["expires_at"]) > now
        ]
        purchased_credit = sum(float(item["amount_usd"]) for item in active_grants)
        available_credit = plan.included_credit_usd + purchased_credit
        budget = plan.cost_per_30_days
        percent = (cost / budget * 100) if budget else None
        return {
            "tenant_id": tenant_id, "plan_id": plan.id,
            "pricing_version": plan.pricing_version,
            "usage_30_day_cost_usd": round(cost, 8),
            "included_credit_usd": plan.included_credit_usd,
            "active_purchased_credit_usd": round(purchased_credit, 8),
            "remaining_credit_usd": round(max(available_credit - cost, 0), 8),
            "active_credit_grants": len(active_grants),
            "budget_usd": budget,
            "budget_used_percent": round(percent, 4) if percent is not None else None,
            "triggered_alert_percentages": [
                threshold for threshold in plan.budget_alert_percentages
                if percent is not None and percent >= threshold
            ],
        }

    @app.get("/admin/tenants/{tenant_id}/credits")
    async def list_tenant_credits(request: Request, tenant_id: str) -> list[dict[str, object]]:
        require_admin(request)
        if not any(item["id"] == tenant_id for item in await identities.list_tenants()):
            raise HTTPException(404, "tenant not found")
        return await identities.list_credits(tenant_id)

    @app.post("/admin/tenants/{tenant_id}/credits", status_code=201)
    async def grant_tenant_credit(
        request: Request, tenant_id: str, grant: CreditGrantCreate,
    ) -> dict[str, object]:
        require_admin(request)
        expires_at = (
            time.time() + grant.expires_in_days * 86400 if grant.expires_in_days else None
        )
        try:
            created = await identities.grant_credit(
                tenant_id, grant.amount_usd, grant.source, expires_at,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        await record_admin_event(
            request, "tenant.credit_granted", tenant_id, metadata={
                "grant_id": str(created["id"]), "source": grant.source,
            },
        )
        return created

    @app.patch("/admin/tenants/{tenant_id}")
    async def update_tenant(request: Request, tenant_id: str, update: TenantUpdate) -> dict[str, object]:
        require_admin(request)
        if update.plan_id is not None and update.plan_id not in plans:
            raise HTTPException(400, "unknown plan_id")
        try:
            tenant = await identities.update_tenant(tenant_id, update.model_dump(exclude_unset=True))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not tenant:
            raise HTTPException(404, "tenant not found")
        await record_admin_event(
            request, "tenant.updated", tenant_id,
            tenant.get("organization_id") if isinstance(tenant.get("organization_id"), str) else None,
            {"changed_fields": ",".join(sorted(update.model_dump(exclude_unset=True)))},
        )
        return tenant

    @app.post("/admin/tenants/{tenant_id}/keys", status_code=201)
    async def issue_key(request: Request, tenant_id: str, key: KeyCreate) -> dict[str, object]:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        if delegated and await identities.tenant_organization(tenant_id) != delegated.organization_id:
            raise HTTPException(403, "organization scope mismatch")
        if delegated and delegated.project_id and key.project_id != delegated.project_id:
            raise HTTPException(403, "project scope mismatch")
        if delegated:
            if key.permissions is None:
                raise HTTPException(403, "delegated key issuance requires an explicit permission ceiling")
            if not set(key.permissions) <= delegated.permissions:
                raise HTTPException(403, "key scope exceeds issuer permissions")
        try:
            issued = await identities.issue_key(tenant_id, **key.model_dump())
            await record_admin_event(
                request, "key.issued", tenant_id, metadata={"key_id": str(issued["id"])},
            )
            return issued
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/admin/tenants/{tenant_id}/keys")
    async def list_keys(request: Request, tenant_id: str) -> list[dict[str, object]]:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        if delegated and await identities.tenant_organization(tenant_id) != delegated.organization_id:
            raise HTTPException(403, "organization scope mismatch")
        return await identities.list_keys(tenant_id, delegated.project_id if delegated else None)

    @app.delete("/admin/keys/{key_id}", status_code=204)
    async def revoke_key(request: Request, key_id: str) -> None:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        if delegated:
            organization_id, project_id = await identities.key_scope(key_id)
            if organization_id != delegated.organization_id:
                raise HTTPException(403, "organization scope mismatch")
            if delegated.project_id and project_id != delegated.project_id:
                raise HTTPException(403, "project scope mismatch")
        if not await identities.revoke_key(key_id):
            raise HTTPException(404, "key not found")
        await record_admin_event(request, "key.revoked", metadata={"key_id": key_id})

    @app.post("/admin/organizations", status_code=201)
    async def create_organization(request: Request, body: NamedIdentityCreate) -> dict[str, object]:
        require_admin(request)
        try:
            created = await identities.create_organization(body.name)
            await record_admin_event(request, "organization.created", organization_id=str(created["id"]))
            return created
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate key" in str(exc).lower():
                raise HTTPException(409, "organization name already exists") from exc
            raise

    @app.get("/admin/organizations")
    async def list_organizations(request: Request) -> list[dict[str, object]]:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        return await identities.list_organizations(delegated.organization_id if delegated else None)

    @app.patch("/admin/organizations/{organization_id}")
    async def update_organization(
        request: Request, organization_id: str, body: NamedIdentityUpdate,
    ) -> dict[str, object]:
        require_admin(request)
        changes = body.changes()
        item = await identities.update_organization(organization_id, changes)
        if not item:
            raise HTTPException(404, "organization not found")
        await record_admin_event(request, "organization.updated", organization_id=organization_id,
                                 metadata={"changed_fields": ",".join(sorted(changes))})
        return item

    @app.post("/admin/organizations/{organization_id}/projects", status_code=201)
    async def create_project(
        request: Request, organization_id: str, body: NamedIdentityCreate,
    ) -> dict[str, object]:
        require_admin(request)
        try:
            created = await identities.create_project(organization_id, body.name)
            await record_admin_event(request, "project.created", organization_id=organization_id,
                                     metadata={"project_id": str(created["id"])})
            return created
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/admin/organizations/{organization_id}/projects")
    async def list_projects(request: Request, organization_id: str) -> list[dict[str, object]]:
        require_admin(request)
        return await identities.list_projects(organization_id)

    @app.patch("/admin/organizations/{organization_id}/projects/{project_id}")
    async def update_project(
        request: Request, organization_id: str, project_id: str, body: NamedIdentityUpdate,
    ) -> dict[str, object]:
        require_admin(request)
        changes = body.changes()
        item = await identities.update_project(organization_id, project_id, changes)
        if not item:
            raise HTTPException(404, "project not found")
        await record_admin_event(request, "project.updated", organization_id=organization_id,
                                 metadata={"project_id": project_id,
                                           "changed_fields": ",".join(sorted(changes))})
        return item

    @app.post("/admin/organizations/{organization_id}/principals", status_code=201)
    async def create_principal(
        request: Request, organization_id: str, body: PrincipalCreate,
    ) -> dict[str, object]:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        if delegated and delegated.project_id and body.project_id != delegated.project_id:
            raise HTTPException(403, "project scope mismatch")
        try:
            created = await identities.create_principal(organization_id, **body.model_dump())
            await record_admin_event(request, "principal.created", organization_id=organization_id,
                                     metadata={"principal_id": str(created["id"]), "kind": body.kind})
            return created
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/admin/organizations/{organization_id}/principals")
    async def list_principals(request: Request, organization_id: str) -> list[dict[str, object]]:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        return await identities.list_principals(
            organization_id, delegated.project_id if delegated else None
        )

    @app.patch("/admin/organizations/{organization_id}/principals/{principal_id}")
    async def update_principal(
        request: Request, organization_id: str, principal_id: str, body: PrincipalUpdate,
    ) -> dict[str, object]:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        if delegated and delegated.project_id:
            if await identities.principal_project(principal_id) != delegated.project_id:
                raise HTTPException(403, "project scope mismatch")
        changes = body.changes()
        item = await identities.update_principal(organization_id, principal_id, changes)
        if not item:
            raise HTTPException(404, "principal not found")
        await record_admin_event(request, "principal.updated", organization_id=organization_id,
                                 metadata={"principal_id": principal_id,
                                           "changed_fields": ",".join(sorted(changes))})
        return item

    @app.post("/admin/organizations/{organization_id}/role-bindings", status_code=201)
    async def assign_role(
        request: Request, organization_id: str, body: RoleBindingCreate,
    ) -> dict[str, object]:
        require_admin(request)
        delegated = getattr(request.state, "admin_auth_context", None)
        if delegated:
            from .authorization import BUILTIN_ROLES

            granted = BUILTIN_ROLES.get(body.role_name)
            if granted is None:
                raise HTTPException(400, "unknown role")
            if not granted <= delegated.permissions:
                raise HTTPException(403, "role assignment exceeds issuer permissions")
        try:
            created = await identities.assign_role(organization_id, **body.model_dump())
            await record_admin_event(request, "role.assigned", organization_id=organization_id,
                                     metadata={"binding_id": str(created["id"]),
                                               "role_name": body.role_name,
                                               "principal_id": body.principal_id})
            return created
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/admin/organizations/{organization_id}/role-bindings")
    async def list_role_bindings(request: Request, organization_id: str) -> list[dict[str, object]]:
        require_admin(request)
        return await identities.list_role_bindings(organization_id)

    @app.delete("/admin/organizations/{organization_id}/role-bindings/{binding_id}", status_code=204)
    async def revoke_role_binding(request: Request, organization_id: str, binding_id: str) -> None:
        require_admin(request)
        if not await identities.revoke_role_binding(organization_id, binding_id):
            raise HTTPException(404, "role binding not found")
        await record_admin_event(request, "role.revoked", organization_id=organization_id,
                                 metadata={"binding_id": binding_id})

    @app.get("/admin/providers")
    async def providers(request: Request) -> list[dict[str, Any]]:
        require_admin(request)
        return request.app.state.router.status()

    @app.get("/admin/audit/events")
    async def audit_events(request: Request, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        require_admin(request)
        bounded = min(max(limit, 1), config.audit.read_max_events)
        return await audit_store.read_after(max(after, 0), bounded)

    @app.get("/admin/audit/verify")
    async def audit_verify(request: Request) -> dict[str, Any]:
        require_admin(request)
        valid, sequence, last_hash = await audit_store.verify()
        if not valid:
            raise HTTPException(409, "audit chain verification failed")
        return {"valid": True, "sequence": sequence, "last_hash": last_hash}

    @app.post("/admin/audit/export")
    async def audit_export(request: Request) -> dict[str, Any]:
        require_admin(request)
        if not config.audit.export_url:
            raise HTTPException(409, "audit export URL is not configured")
        token = os.getenv(config.audit.export_token_env, "") if config.audit.export_token_env else ""

        async def sink(events: list[dict[str, Any]]) -> None:
            headers = {"Idempotency-Key": events[-1]["event_hash"]}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=config.audit.export_timeout_seconds) as client:
                response = await client.post(config.audit.export_url, json={"events": events}, headers=headers)
                response.raise_for_status()

        try:
            count = await audit_store.export("default", config.audit.export_batch_size, sink)
        except Exception as exc:
            await metrics.security("audit_export", "failure")
            raise HTTPException(502, "audit export failed; checkpoint was not advanced") from exc
        await metrics.security("audit_export", "success")
        return {"exported": count, "sink_id": "default"}

    @app.post("/admin/encryption/rotate")
    async def rotate_encryption(request: Request) -> dict[str, Any]:
        require_admin(request)
        from .encryption import rotate_postgres, rotate_sqlite

        try:
            if config.database.startswith(("postgresql://", "postgres://")):
                pool = next((
                    getattr(store, "pool", None)
                    for store in (shared_store, idempotency_store, cache_store)
                    if store is not None and getattr(store, "pool", None) is not None
                ), None)
                if pool is None:
                    return {"rotated": 0, "active_key_version": envelope_cipher.active_version}
                result = await rotate_postgres(
                    pool, envelope_cipher, config.encryption.rotation_batch_size,
                    config.encryption.allow_plaintext_migration,
                )
            else:
                result = await asyncio.to_thread(
                    rotate_sqlite, config.database, envelope_cipher,
                    config.encryption.rotation_batch_size,
                    config.encryption.allow_plaintext_migration,
                )
        except Exception as exc:
            await metrics.security("encryption_rotation", "failure")
            raise HTTPException(409, "encryption rotation failed; batch was rolled back") from exc
        await metrics.security("encryption_rotation", "success")
        await record_admin_event(
            request, "encryption.rotated",
            metadata={"records_rotated": result["rotated"],
                      "active_key_version": envelope_cipher.active_version},
        )
        return {**result, "active_key_version": envelope_cipher.active_version}

    @app.get("/admin/config/diagnostics")
    async def config_diagnostics(request: Request) -> dict[str, object]:
        require_admin(request)
        active_config = request.app.state.active_config
        issues = [
            {"severity": severity, "message": message}
            for severity, message in active_config.diagnostics()
        ]
        return {"valid": not any(item["severity"] == "ERROR" for item in issues), "issues": issues}

    if config.features.config_reload:
        @app.post("/admin/config/reload")
        async def reload_config(request: Request) -> dict[str, object]:
            nonlocal reload_generation
            require_admin(request)
            try:
                candidate = load_config(config_path)
            except Exception as exc:
                logger.warning(
                    "Configuration reload rejected error_type=%s", type(exc).__name__
                )
                raise HTTPException(422, "configuration reload rejected") from exc
            if _reload_immutable_state(candidate) != _reload_immutable_state(config):
                raise HTTPException(
                    409,
                    "configuration reload supports only routing and provider/model changes",
                )
            async with reload_lock:
                if reload_generation >= config.config_reload.max_generations:
                    raise HTTPException(409, "configuration reload generation limit reached; restart required")
                client = _provider_client(candidate)
                try:
                    router = GatewayRouter(candidate, quota, client, metrics, telemetry, coordinator)
                except Exception:
                    await client.aclose()
                    raise
                reload_clients.append(client)
                request.app.state.router = router
                request.app.state.active_config = candidate
                reload_generation += 1
            logger.info("Configuration routing state reloaded generation=%s", reload_generation)
            return {
                "status": "reloaded",
                "generation": reload_generation,
                "providers": len(candidate.providers),
            }

    @app.post("/admin/model-discovery")
    async def model_discovery(request: Request, selection: ModelDiscoveryRequest) -> dict[str, object]:
        require_admin(request)
        active_config = request.app.state.active_config
        requested = set(selection.providers) if selection.providers is not None else None
        known = {provider.name for provider in active_config.providers}
        if requested is not None:
            if any(not name or len(name) > 120 for name in requested):
                raise HTTPException(400, "provider names must be bounded non-empty text")
            unknown = sorted(requested - known)
            if unknown:
                raise HTTPException(400, f"unknown provider: {unknown[0]}")
        from .model_discovery import discover_models

        reports = await discover_models(active_config, request.app.state.router.client, requested)
        return {
            "object": "model_discovery_report",
            "approval_mode": active_config.model_discovery.approval_mode,
            "providers": reports,
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus(request: Request) -> str:
        require_admin(request)
        return await metrics.render()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        context = await require_api(request)
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "request body must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        injection_metadata: dict[str, object] = {}
        require_injection_safe = False
        injection_report = None
        if injection_processor is not None:
            from .prompt_injection import PromptInjectionBlocked, PromptInjectionError

            try:
                injection_report = injection_processor.process(payload)
                require_injection_safe = injection_report.route_required
                await metrics.prompt_injection(
                    injection_report.action,
                    "risky" if injection_report.risky else "clean",
                )
                injection_metadata = injection_report.metadata()
                if injection_report.risky:
                    log = logger.warning if injection_report.action == "warn" else logger.info
                    log(
                        "Prompt-injection risk action=%s score=%.4f signals=%s",
                        injection_report.action, injection_report.score,
                        ",".join(injection_report.signals),
                    )
            except PromptInjectionBlocked as exc:
                await metrics.prompt_injection("block", "blocked")
                logger.warning(
                    "Prompt-injection policy blocked request score=%.4f signals=%s",
                    exc.report.score, ",".join(exc.report.signals),
                )
                raise HTTPException(400, "request blocked by prompt-injection policy") from exc
            except PromptInjectionError as exc:
                await metrics.prompt_injection(config.prompt_injection.action, "error")
                logger.error("Prompt-injection evaluation failed error_type=%s", type(exc).__name__)
                if config.prompt_injection.failure_mode == "closed":
                    raise HTTPException(
                        422, "request could not be evaluated by prompt-injection policy"
                    ) from exc
                injection_metadata = {"action": "bypass", "reason": "evaluation_error"}
        moderation_metadata: dict[str, object] = {}
        if moderation_processor is not None and config.moderation.scan in {"input", "both"}:
            from .moderation import ModerationBlocked, ModerationError

            try:
                moderation_report = moderation_processor.process_request(payload)
                await metrics.moderation(
                    "input", moderation_report.action,
                    "flagged" if moderation_report.flagged else "clean",
                )
                moderation_metadata["input"] = moderation_report.metadata()
                if moderation_report.flagged:
                    logger.info(
                        "Moderation policy flagged request action=%s findings=%s categories=%s",
                        moderation_report.action, moderation_report.findings,
                        ",".join(moderation_report.categories),
                    )
            except ModerationBlocked as exc:
                await metrics.moderation("input", "block", "blocked")
                logger.warning(
                    "Moderation policy blocked request findings=%s categories=%s",
                    exc.report.findings, ",".join(exc.report.categories),
                )
                raise HTTPException(400, "request blocked by moderation policy") from exc
            except ModerationError as exc:
                await metrics.moderation("input", config.moderation.action, "error")
                logger.error("Moderation request evaluation failed error_type=%s", type(exc).__name__)
                if config.moderation.failure_mode == "closed":
                    raise HTTPException(422, "request could not be evaluated by moderation policy") from exc
                moderation_metadata["input"] = {"action": "bypass", "reason": "evaluation_error"}
        pii_metadata: dict[str, object] = {}
        if pii_processor is not None and config.pii.scan in {"input", "both"}:
            from .pii import PIIBlocked, PIIScanError

            try:
                payload, pii_report = pii_processor.process_request(payload)
                await metrics.pii(
                    "input", pii_report.action,
                    "matched" if pii_report.matches else "clean",
                )
                pii_metadata["input"] = pii_report.metadata()
                if pii_report.matches:
                    logger.info(
                        "PII policy processed request action=%s matches=%s types=%s",
                        pii_report.action, pii_report.matches, ",".join(pii_report.types),
                    )
            except PIIBlocked as exc:
                await metrics.pii("input", "block", "blocked")
                logger.warning(
                    "PII policy blocked request matches=%s types=%s",
                    exc.report.matches, ",".join(exc.report.types),
                )
                raise HTTPException(
                    400, "request blocked by PII policy",
                ) from exc
            except PIIScanError as exc:
                await metrics.pii("input", config.pii.action, "error")
                logger.error("PII request scan failed error_type=%s", type(exc).__name__)
                if config.pii.failure_mode == "closed":
                    raise HTTPException(422, "request could not be evaluated by PII policy") from exc
                pii_metadata["input"] = {"action": "bypass", "reason": "scan_error"}
        idempotency_key = request.headers.get("idempotency-key")
        if idempotency_key and not config.features.idempotency:
            raise HTTPException(400, "idempotency is not enabled")
        if idempotency_key and not (
            config.idempotency.min_key_length <= len(idempotency_key) <= config.idempotency.max_key_length
            and all(33 <= ord(character) <= 126 for character in idempotency_key)
        ):
            raise HTTPException(400, "invalid Idempotency-Key")
        router: GatewayRouter = request.app.state.router
        tenant_limits = context.limits if config.features.tenant_quotas else ()
        if payload.get("stream"):
            if (
                moderation_processor is not None
                and config.moderation.scan in {"output", "both"}
                and config.moderation.streaming_output == "block"
            ):
                raise HTTPException(400, "streaming is disabled when output moderation is required")
            if (
                pii_processor is not None
                and config.pii.scan in {"output", "both"}
                and config.pii.streaming_output == "block"
            ):
                raise HTTPException(400, "streaming is disabled when PII output scanning is required")
            if idempotency_key:
                raise HTTPException(400, "Idempotency-Key is not supported for streaming requests")
            iterator = router.stream(
                payload, context.tenant_id, tenant_limits, require_injection_safe
            )
            try:
                first = await anext(iterator)
            except QuotaExceeded as exc:
                raise HTTPException(429, str(exc)) from exc
            except NoModelAvailable as exc:
                raise HTTPException(503, str(exc)) from exc
            except StopAsyncIteration:
                first = b""

            async def replay():
                if first:
                    yield first
                async for chunk in iterator:
                    yield chunk

            response = StreamingResponse(replay(), media_type="text/event-stream")
            if injection_report is not None:
                response.headers["X-Prompt-Injection-Score"] = f"{injection_report.score:.4f}"
                if injection_report.risky and injection_report.action == "warn":
                    response.headers["X-Prompt-Injection-Risk"] = "warning"
            if moderation_processor is not None and config.moderation.scan in {"output", "both"}:
                response.headers["X-Moderation-Output"] = "BYPASS"
            if pii_processor is not None and config.pii.scan in {"output", "both"}:
                response.headers["X-PII-Output"] = "BYPASS"
            if cache_store is not None:
                response.headers["X-Cache"] = "BYPASS"
                await metrics.cache(config.cache.backend, "bypass")
            return response
        idempotency_digest = None
        owner_token = None
        if idempotency_key and idempotency_store is not None:
            from .idempotency import key_hash, request_hash

            idempotency_digest = key_hash(idempotency_key)
            fingerprint = request_hash(payload)
            wait_deadline = time.monotonic() + config.idempotency.wait_timeout_seconds
            while True:
                claim = await idempotency_store.claim(
                    context.tenant_id, idempotency_digest, fingerprint
                )
                if claim.status == "conflict":
                    raise HTTPException(409, "Idempotency-Key was already used with a different request")
                if claim.status == "cached":
                    return JSONResponse(
                        content=claim.response,
                        headers={"Idempotent-Replayed": "true"},
                    )
                if claim.status == "owner":
                    owner_token = claim.owner_token
                    break
                if time.monotonic() >= wait_deadline:
                    raise HTTPException(
                        409, "request with this Idempotency-Key is still in progress",
                        headers={"Retry-After": "1"},
                    )
                await asyncio.sleep(config.idempotency.poll_interval_seconds)
        cache_key = None
        cache_allowed = cache_store is not None and cache_available
        cache_control = request.headers.get("cache-control", "").lower()
        if config.cache.allow_client_bypass and any(
            directive.strip() in {"no-cache", "no-store"}
            for directive in cache_control.split(",")
        ):
            cache_allowed = False
        if cache_store is not None and not cache_allowed:
            await metrics.cache(config.cache.backend, "bypass")
        if cache_allowed:
            from .cache import exact_cache_key

            cache_key = exact_cache_key(
                payload, config.cache.namespace, cache_routing_signature
            )
            try:
                cached = await cache_store.get(context.tenant_id, cache_key)
            except Exception as exc:
                if config.cache.failure_mode == "closed":
                    if owner_token and idempotency_digest:
                        await idempotency_store.release(
                            context.tenant_id, idempotency_digest, owner_token
                        )
                    raise
                cached = None
                cache_allowed = False
                await metrics.cache(config.cache.backend, "bypass")
                logger.error(
                    "Exact cache lookup failed; bypassing cache error_type=%s",
                    type(exc).__name__,
                )
            if cached is not None:
                await metrics.cache(config.cache.backend, "hit")
                headers = {
                    "X-Cache": "HIT",
                    "Age": str(max(0, int(time.time() - cached.created_at))),
                }
                if owner_token and idempotency_digest:
                    encoded_cached = json.dumps(
                        cached.response, ensure_ascii=False, separators=(",", ":")
                    )
                    stored = await idempotency_store.complete(
                        context.tenant_id, idempotency_digest, owner_token, encoded_cached
                    )
                    headers.update({
                        "Idempotent-Replayed": "false",
                        "Idempotency-Stored": str(stored).lower(),
                    })
                return JSONResponse(content=cached.response, headers=headers)
        semantic_allowed = semantic_cache_store is not None
        if semantic_allowed:
            semantic_cached = await semantic_cache_store.get(context.tenant_id, payload)
            if semantic_cached is not None:
                headers = {"X-Semantic-Cache": "HIT"}
                if owner_token and idempotency_digest:
                    encoded_semantic = json.dumps(
                        semantic_cached, ensure_ascii=False, separators=(",", ":")
                    )
                    stored = await idempotency_store.complete(
                        context.tenant_id, idempotency_digest, owner_token, encoded_semantic
                    )
                    headers.update({
                        "Idempotent-Replayed": "false",
                        "Idempotency-Stored": str(stored).lower(),
                    })
                return JSONResponse(content=semantic_cached, headers=headers)
        try:
            body, provider, upstream_model = await router.complete_for(
                payload, context.tenant_id, tenant_limits, require_injection_safe
            )
        except QuotaExceeded as exc:
            if owner_token and idempotency_digest:
                await idempotency_store.release(context.tenant_id, idempotency_digest, owner_token)
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except NoModelAvailable as exc:
            if owner_token and idempotency_digest:
                await idempotency_store.release(context.tenant_id, idempotency_digest, owner_token)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception:
            if owner_token and idempotency_digest:
                await idempotency_store.release(context.tenant_id, idempotency_digest, owner_token)
            raise
        meta = body.pop("_gateway_meta", {})
        if moderation_processor is not None and config.moderation.scan in {"output", "both"}:
            from .moderation import ModerationBlocked, ModerationError

            try:
                moderation_report = moderation_processor.process_response(body)
                await metrics.moderation(
                    "output", moderation_report.action,
                    "flagged" if moderation_report.flagged else "clean",
                )
                moderation_metadata["output"] = moderation_report.metadata()
                if moderation_report.flagged:
                    logger.info(
                        "Moderation policy flagged response action=%s findings=%s categories=%s",
                        moderation_report.action, moderation_report.findings,
                        ",".join(moderation_report.categories),
                    )
            except ModerationBlocked as exc:
                await metrics.moderation("output", "block", "blocked")
                if owner_token and idempotency_digest:
                    await idempotency_store.release(
                        context.tenant_id, idempotency_digest, owner_token
                    )
                logger.warning(
                    "Moderation policy blocked response findings=%s categories=%s",
                    exc.report.findings, ",".join(exc.report.categories),
                )
                raise HTTPException(502, "upstream response blocked by moderation policy") from exc
            except ModerationError as exc:
                await metrics.moderation("output", config.moderation.action, "error")
                logger.error("Moderation response evaluation failed error_type=%s", type(exc).__name__)
                if config.moderation.failure_mode == "closed":
                    if owner_token and idempotency_digest:
                        await idempotency_store.release(
                            context.tenant_id, idempotency_digest, owner_token
                        )
                    raise HTTPException(
                        502, "upstream response could not be evaluated by moderation policy"
                    ) from exc
                moderation_metadata["output"] = {
                    "action": "bypass", "reason": "evaluation_error"
                }
        if pii_processor is not None and config.pii.scan in {"output", "both"}:
            from .pii import PIIBlocked, PIIScanError

            try:
                body, pii_report = pii_processor.process_response(body)
                await metrics.pii(
                    "output", pii_report.action,
                    "matched" if pii_report.matches else "clean",
                )
                pii_metadata["output"] = pii_report.metadata()
                if pii_report.matches:
                    logger.info(
                        "PII policy processed response action=%s matches=%s types=%s",
                        pii_report.action, pii_report.matches, ",".join(pii_report.types),
                    )
            except PIIBlocked as exc:
                await metrics.pii("output", "block", "blocked")
                if owner_token and idempotency_digest:
                    await idempotency_store.release(
                        context.tenant_id, idempotency_digest, owner_token
                    )
                logger.warning(
                    "PII policy blocked response matches=%s types=%s",
                    exc.report.matches, ",".join(exc.report.types),
                )
                raise HTTPException(502, "upstream response blocked by PII policy") from exc
            except PIIScanError as exc:
                await metrics.pii("output", config.pii.action, "error")
                logger.error("PII response scan failed error_type=%s", type(exc).__name__)
                if config.pii.failure_mode == "closed":
                    if owner_token and idempotency_digest:
                        await idempotency_store.release(
                            context.tenant_id, idempotency_digest, owner_token
                        )
                    raise HTTPException(502, "upstream response could not be evaluated by PII policy") from exc
                pii_metadata["output"] = {"action": "bypass", "reason": "scan_error"}
        result = {
            **body,
            "gateway": {
                "provider": provider, "upstream_model": upstream_model, **meta,
                **({"pii": pii_metadata} if pii_metadata else {}),
                **({"moderation": moderation_metadata} if moderation_metadata else {}),
                **({"prompt_injection": injection_metadata} if injection_metadata else {}),
            },
        }
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        response_headers = {}
        if semantic_allowed and len(encoded.encode("utf-8")) <= config.cache.max_response_bytes:
            semantic_result = {
                **result,
                "gateway": {**result["gateway"], "rate_limits": {}},
            }
            await semantic_cache_store.put(context.tenant_id, payload, semantic_result)
            response_headers["X-Semantic-Cache"] = "MISS"
        if cache_store is not None:
            if cache_allowed:
                await metrics.cache(config.cache.backend, "miss")
            if cache_allowed and cache_key and len(encoded.encode("utf-8")) <= config.cache.max_response_bytes:
                cache_result = {
                    **result,
                    "gateway": {**result["gateway"], "rate_limits": {}},
                }
                try:
                    await cache_store.put(
                        context.tenant_id, cache_key,
                        json.dumps(cache_result, ensure_ascii=False, separators=(",", ":")),
                    )
                    response_headers["X-Cache"] = "MISS"
                except Exception as exc:
                    if config.cache.failure_mode == "closed":
                        if owner_token and idempotency_digest:
                            await idempotency_store.release(
                                context.tenant_id, idempotency_digest, owner_token
                            )
                        raise
                    logger.error(
                        "Exact cache write failed; returning uncached response error_type=%s",
                        type(exc).__name__,
                    )
                    response_headers["X-Cache"] = "BYPASS"
                    response_headers["X-Cache-Reason"] = "backend-error"
            else:
                response_headers["X-Cache"] = "BYPASS"
                if cache_allowed:
                    response_headers["X-Cache-Reason"] = "response-too-large"
        if owner_token and idempotency_digest:
            if len(encoded.encode("utf-8")) <= config.idempotency.max_response_bytes:
                stored = await idempotency_store.complete(
                    context.tenant_id, idempotency_digest, owner_token, encoded
                )
            else:
                await idempotency_store.release(context.tenant_id, idempotency_digest, owner_token)
                stored = False
            response_headers.update({
                "Idempotent-Replayed": "false", "Idempotency-Stored": str(stored).lower()
            })
        if response_headers:
            return JSONResponse(content=result, headers=response_headers)
        return result

    @app.post("/v1/responses")
    async def responses(request: Request):
        await require_api(request)
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "request body must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        from .responses_api import ResponsesAdapterError, from_chat_response, to_chat_payload

        try:
            chat_payload = to_chat_payload(payload, config.responses)
        except ResponsesAdapterError as exc:
            raise HTTPException(400, str(exc)) from exc
        chat_result = await chat(request_with_json(request, chat_payload))
        status_code = 200
        headers: dict[str, str] = {}
        if isinstance(chat_result, JSONResponse):
            status_code = chat_result.status_code
            body = json.loads(chat_result.body)
            headers = {
                name: value for name, value in chat_result.headers.items()
                if name.lower() not in {"content-length", "content-type"}
            }
        else:
            body = chat_result
        try:
            response_body = from_chat_response(body)
        except ResponsesAdapterError as exc:
            raise HTTPException(502, "upstream response could not be adapted") from exc
        response_body["metadata"] = payload.get("metadata") or {}
        response_body["parallel_tool_calls"] = payload.get("parallel_tool_calls", True)
        return JSONResponse(content=response_body, status_code=status_code, headers=headers)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        context = await require_api(request)
        if request.headers.get("idempotency-key"):
            raise HTTPException(400, "Idempotency-Key is not supported for embeddings requests")
        try:
            raw_payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "request body must contain valid JSON") from exc
        if not isinstance(raw_payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        from .embeddings_api import EmbeddingsValidationError, validate_embeddings_payload

        try:
            payload = validate_embeddings_payload(raw_payload, config.embeddings)
        except EmbeddingsValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        policy_metadata: dict[str, object] = {}
        if moderation_processor is not None and config.moderation.scan in {"input", "both"}:
            from .moderation import ModerationBlocked, ModerationError

            try:
                report = moderation_processor.process_request(payload)
                await metrics.moderation(
                    "input", report.action, "flagged" if report.flagged else "clean"
                )
                policy_metadata["moderation"] = {"input": report.metadata()}
                if report.flagged:
                    logger.info(
                        "Moderation policy flagged embeddings input action=%s findings=%s categories=%s",
                        report.action, report.findings, ",".join(report.categories),
                    )
            except ModerationBlocked as exc:
                await metrics.moderation("input", "block", "blocked")
                logger.warning(
                    "Moderation policy blocked embeddings input findings=%s categories=%s",
                    exc.report.findings, ",".join(exc.report.categories),
                )
                raise HTTPException(400, "request blocked by moderation policy") from exc
            except ModerationError as exc:
                await metrics.moderation("input", config.moderation.action, "error")
                if config.moderation.failure_mode == "closed":
                    raise HTTPException(422, "request could not be evaluated by moderation policy") from exc
                policy_metadata["moderation"] = {
                    "input": {"action": "bypass", "reason": "evaluation_error"}
                }
        if pii_processor is not None and config.pii.scan in {"input", "both"}:
            from .pii import PIIBlocked, PIIScanError

            try:
                payload, report = pii_processor.process_request(payload)
                await metrics.pii(
                    "input", report.action, "matched" if report.matches else "clean"
                )
                policy_metadata["pii"] = {"input": report.metadata()}
                if report.matches:
                    logger.info(
                        "PII policy processed embeddings input action=%s matches=%s types=%s",
                        report.action, report.matches, ",".join(report.types),
                    )
            except PIIBlocked as exc:
                await metrics.pii("input", "block", "blocked")
                logger.warning(
                    "PII policy blocked embeddings input matches=%s types=%s",
                    exc.report.matches, ",".join(exc.report.types),
                )
                raise HTTPException(400, "request blocked by PII policy") from exc
            except PIIScanError as exc:
                await metrics.pii("input", config.pii.action, "error")
                if config.pii.failure_mode == "closed":
                    raise HTTPException(422, "request could not be evaluated by PII policy") from exc
                policy_metadata["pii"] = {
                    "input": {"action": "bypass", "reason": "scan_error"}
                }
        router: GatewayRouter = request.app.state.router
        tenant_limits = context.limits if config.features.tenant_quotas else ()
        try:
            body, _provider, _upstream_model = await router.embed_for(
                payload, context.tenant_id, tenant_limits
            )
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc)) from exc
        except NoModelAvailable as exc:
            raise HTTPException(503, str(exc)) from exc
        if policy_metadata:
            body.setdefault("gateway", {})["policies"] = policy_metadata
        return body

    @app.post("/v1/moderations")
    async def moderations(request: Request):
        await require_api(request)
        if request.headers.get("idempotency-key"):
            raise HTTPException(400, "Idempotency-Key is not supported for moderation requests")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "request body must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        from .moderation import ModerationError
        from .moderations_api import (
            ModerationsValidationError, moderation_response, validate_moderations_payload,
        )

        try:
            model, inputs = validate_moderations_payload(payload, config.moderations_api)
        except ModerationsValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        reports = []
        try:
            assert moderation_api_processor is not None
            for input_text in inputs:
                report = moderation_api_processor.process_request({"input": input_text})
                reports.append(report)
                await metrics.moderation(
                    "api", "classify", "flagged" if report.flagged else "clean"
                )
        except ModerationError as exc:
            await metrics.moderation("api", "classify", "error")
            logger.error("Moderation API evaluation failed error_type=%s", type(exc).__name__)
            raise HTTPException(503, "moderation engine could not evaluate the request") from exc
        return moderation_response(model, reports)

    @app.post("/v1/audio/speech")
    async def audio_speech(request: Request):
        context = await require_api(request)
        if request.headers.get("idempotency-key"):
            raise HTTPException(400, "Idempotency-Key is not supported for speech requests")
        try:
            raw_payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "request body must contain valid JSON") from exc
        if not isinstance(raw_payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        from .audio_api import AudioValidationError, CONTENT_TYPES, validate_speech_payload

        try:
            payload = validate_speech_payload(raw_payload, config.audio)
        except AudioValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        router: GatewayRouter = request.app.state.router
        tenant_limits = context.limits if config.features.tenant_quotas else ()
        try:
            content, provider, upstream_model, upstream_headers = await router.speech_for(
                payload, context.tenant_id, tenant_limits
            )
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc)) from exc
        except NoModelAvailable as exc:
            raise HTTPException(503, str(exc)) from exc
        headers = {
            "X-Gateway-Provider": provider,
            "X-Gateway-Upstream-Model": upstream_model,
        }
        for name, value in upstream_headers.items():
            if name.lower().startswith(("x-ratelimit", "ratelimit")):
                headers[name] = value
            elif name.lower() == "x-gateway-routing-policy":
                headers[name] = value
        return Response(
            content=content,
            media_type=CONTENT_TYPES[payload["response_format"]],
            headers=headers,
        )

    @app.post("/v1/images/generations")
    async def image_generations(request: Request):
        context = await require_api(request)
        if request.headers.get("idempotency-key"):
            raise HTTPException(400, "Idempotency-Key is not supported for image requests")
        try:
            raw_payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "request body must contain valid JSON") from exc
        if not isinstance(raw_payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        from .images_api import ImagesValidationError, validate_image_payload

        try:
            payload = validate_image_payload(raw_payload, config.images)
        except ImagesValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        router: GatewayRouter = request.app.state.router
        tenant_limits = context.limits if config.features.tenant_quotas else ()
        try:
            body, _provider, _upstream_model = await router.generate_images_for(
                payload, context.tenant_id, tenant_limits
            )
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc)) from exc
        except NoModelAvailable as exc:
            raise HTTPException(503, str(exc)) from exc
        return body

    disabled_paths: set[str] = set()
    if not config.features.admin_api:
        disabled_paths.update(
            route.path for route in app.routes
            if getattr(route, "path", "").startswith("/admin/")
        )
    else:
        if not config.features.managed_keys:
            disabled_paths.update(
                route.path for route in app.routes
                if getattr(route, "path", "").startswith(("/admin/tenants", "/admin/keys"))
            )
        if not config.features.identity_hierarchy:
            disabled_paths.update(
                route.path for route in app.routes
                if getattr(route, "path", "").startswith("/admin/organizations")
            )
        if not config.features.usage_accounting:
            disabled_paths.update({"/admin/usage", "/admin/usage/tenants"})
        if not config.features.managed_keys or not config.features.tenant_quotas:
            disabled_paths.add("/admin/usage/tenants")
        if not config.features.finops_plans:
            disabled_paths.update({
                "/admin/plans", "/admin/tenants/{tenant_id}/budget",
                "/admin/tenants/{tenant_id}/credits",
            })
        if not config.features.finops_export:
            disabled_paths.update({
                "/admin/usage/export", "/admin/usage/reconcile",
                "/admin/usage/export-deliver",
            })
    if not config.features.metrics:
        disabled_paths.add("/metrics")
    if not config.features.responses_api:
        disabled_paths.add("/v1/responses")
    if not config.features.embeddings_api:
        disabled_paths.add("/v1/embeddings")
    if not config.features.moderations_api:
        disabled_paths.add("/v1/moderations")
    if not config.features.audio_api:
        disabled_paths.add("/v1/audio/speech")
    if not config.features.images_api:
        disabled_paths.add("/v1/images/generations")
    if not config.features.async_jobs:
        disabled_paths.add("/v1/jobs/{job_id}")
    if not config.features.batch_api:
        disabled_paths.update({"/v1/batches", "/v1/batches/{job_id}"})
    if not config.features.model_discovery:
        disabled_paths.add("/admin/model-discovery")
    if not config.features.audit_ledger:
        disabled_paths.update(
            route.path for route in app.routes
            if getattr(route, "path", "").startswith("/admin/audit")
        )
    if not config.features.envelope_encryption:
        disabled_paths.add("/admin/encryption/rotate")
    app.router.routes = [
        route for route in app.router.routes
        if getattr(route, "path", None) not in disabled_paths
    ]
    return app


def app_from_env() -> FastAPI:
    return create_app(os.getenv("LLM_GATEWAY_CONFIG", "config.yaml"))


def __getattr__(name: str):
    """Create the ASGI application only when an ASGI server requests it.

    Importing helpers such as ``create_app`` must not require an operator's
    configuration file to exist in the current working directory.  Uvicorn's
    existing ``llm_gateway.app:app`` target remains supported through the
    module attribute hook defined by PEP 562.
    """
    if name == "app":
        return app_from_env()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import hashlib
import logging
import time
from typing import Any
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import load_config
from .metrics import Metrics, NullMetrics
from .identity import AuthContext, IdentityStore
from .logging_config import begin_request, bind_tenant, end_request
from .quota import NullQuotaStore, QuotaStore
from .router import GatewayRouter, NoModelAvailable, QuotaExceeded

logger = logging.getLogger("llm_gateway")


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_day: int | None = Field(default=None, gt=0)
    cost_per_30_days: float | None = Field(default=None, gt=0)


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, gt=0, le=3650)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_day: int | None = Field(default=None, gt=0)
    cost_per_30_days: float | None = Field(default=None, gt=0)


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


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    quota_enabled = bool(
        config.features.provider_quotas
        or config.features.tenant_quotas
        or config.features.usage_accounting
    )
    identity_enabled = bool(config.features.managed_keys)
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
            config.idempotency.lease_seconds,
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
                config.cache.redis_url() or "", config.cache.ttl_seconds, config.cache.redis_prefix
            )
        elif config.database.startswith(("postgresql://", "postgres://")):
            cache_store = PostgresCacheStore(
                config.database, config.cache.ttl_seconds, config.cache.max_entries
            )
        else:
            cache_store = SQLiteCacheStore(
                config.database, config.cache.ttl_seconds, config.cache.max_entries
            )
        routing_material = {
            "paid_fallback": config.routing.paid_fallback,
            "providers": [
                {
                    "name": provider.name, "enabled": provider.enabled, "tier": provider.tier,
                    "base_url": provider.base_url, "path": provider.chat_completions_path,
                    "defaults": provider.request_defaults,
                    "models": [
                        {
                            "name": model.name, "upstream": model.upstream_model,
                            "priority": model.priority, "capabilities": sorted(model.capabilities),
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
        removed = await quota.cleanup(
            time.time() - config.accounting.retention_days * 86400
        )
        if removed:
            logger.info("Removed %s expired accounting records", removed)
        limits = httpx.Limits(
            max_connections=config.routing.http_max_connections,
            max_keepalive_connections=config.routing.http_max_keepalive_connections,
            keepalive_expiry=config.routing.http_keepalive_expiry_seconds,
        )
        async with httpx.AsyncClient(follow_redirects=True, limits=limits) as client:
            app.state.router = GatewayRouter(config, quota, client, metrics, telemetry)
            yield
        close = getattr(shared_store or quota, "close", None)
        if close:
            await close()
        if idempotency_store is not None:
            await idempotency_store.close()
        if cache_store is not None:
            await cache_store.close()
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

    async def require_api(request: Request) -> AuthContext:
        if not config.features.authentication:
            context = AuthContext("anonymous", "anonymous")
            request.state.tenant_id = context.tenant_id
            if config.features.structured_logging:
                bind_tenant(context.tenant_id)
            if telemetry is not None:
                telemetry.set_tenant(context.tenant_id)
            return context
        token = bearer(request)
        for key in config.auth.api_keys():
            if secrets.compare_digest(token, key):
                identity = hashlib.sha256(key.encode()).hexdigest()[:16]
                context = AuthContext(f"bootstrap-{identity}", f"bootstrap-{identity}")
                request.state.tenant_id = context.tenant_id
                if config.features.structured_logging:
                    bind_tenant(context.tenant_id)
                if telemetry is not None:
                    telemetry.set_tenant(context.tenant_id)
                return context
        if config.features.managed_keys and identities is not None and token:
            context = await identities.authenticate(token)
            if context:
                request.state.tenant_id = context.tenant_id
                if config.features.structured_logging:
                    bind_tenant(context.tenant_id)
                if telemetry is not None:
                    telemetry.set_tenant(context.tenant_id)
                return context
        raise HTTPException(401, "invalid gateway API key", headers={"WWW-Authenticate": "Bearer"})

    def require_admin(request: Request) -> None:
        if not config.features.authentication:
            return
        expected = config.auth.admin_key()
        if not expected or not secrets.compare_digest(bearer(request), expected):
            raise HTTPException(403, "administrator credentials required")

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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "name": "LLM Gateway", "version": __version__, "status": "ok",
            "endpoints": {"health": "/health", "ready": "/ready", "models": "/v1/models", "docs": "/docs" if config.server.docs_enabled else None},
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

    @app.post("/admin/tenants", status_code=201)
    async def create_tenant(request: Request, tenant: TenantCreate) -> dict[str, object]:
        require_admin(request)
        try:
            return await identities.create_tenant(**tenant.model_dump())
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate key" in str(exc).lower():
                raise HTTPException(409, "tenant name already exists") from exc
            raise

    @app.get("/admin/tenants")
    async def list_tenants(request: Request) -> list[dict[str, object]]:
        require_admin(request)
        return await identities.list_tenants()

    @app.patch("/admin/tenants/{tenant_id}")
    async def update_tenant(request: Request, tenant_id: str, update: TenantUpdate) -> dict[str, object]:
        require_admin(request)
        tenant = await identities.update_tenant(tenant_id, update.model_dump(exclude_unset=True))
        if not tenant:
            raise HTTPException(404, "tenant not found")
        return tenant

    @app.post("/admin/tenants/{tenant_id}/keys", status_code=201)
    async def issue_key(request: Request, tenant_id: str, key: KeyCreate) -> dict[str, object]:
        require_admin(request)
        try:
            return await identities.issue_key(tenant_id, **key.model_dump())
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/admin/tenants/{tenant_id}/keys")
    async def list_keys(request: Request, tenant_id: str) -> list[dict[str, object]]:
        require_admin(request)
        return await identities.list_keys(tenant_id)

    @app.delete("/admin/keys/{key_id}", status_code=204)
    async def revoke_key(request: Request, key_id: str) -> None:
        require_admin(request)
        if not await identities.revoke_key(key_id):
            raise HTTPException(404, "key not found")

    @app.get("/admin/providers")
    async def providers(request: Request) -> list[dict[str, Any]]:
        require_admin(request)
        return request.app.state.router.status()

    @app.get("/admin/config/diagnostics")
    async def config_diagnostics(request: Request) -> dict[str, object]:
        require_admin(request)
        issues = [{"severity": severity, "message": message} for severity, message in config.diagnostics()]
        return {"valid": not any(item["severity"] == "ERROR" for item in issues), "issues": issues}

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
            if idempotency_key:
                raise HTTPException(400, "Idempotency-Key is not supported for streaming requests")
            iterator = router.stream(payload, context.tenant_id, tenant_limits)
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
        try:
            body, provider, upstream_model = await router.complete_for(
                payload, context.tenant_id, tenant_limits
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
        result = {**body, "gateway": {"provider": provider, "upstream_model": upstream_model, **meta}}
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        response_headers = {}
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
        if not config.features.usage_accounting:
            disabled_paths.update({"/admin/usage", "/admin/usage/tenants"})
        if not config.features.managed_keys or not config.features.tenant_quotas:
            disabled_paths.add("/admin/usage/tenants")
    if not config.features.metrics:
        disabled_paths.add("/metrics")
    app.router.routes = [
        route for route in app.router.routes
        if getattr(route, "path", None) not in disabled_paths
    ]
    return app


def app_from_env() -> FastAPI:
    import os
    return create_app(os.getenv("LLM_GATEWAY_CONFIG", "config.yaml"))


app = app_from_env()

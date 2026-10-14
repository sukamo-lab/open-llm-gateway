from __future__ import annotations

import json
import logging
import random
import uuid
import asyncio
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator

import httpx

from .config import GatewayConfig, ModelConfig, ProviderConfig
from .quota import QuotaStore, Reservation
from .metrics import Metrics


FAILOVER_STATUS = {401, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504}
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
logger = logging.getLogger("llm_gateway.router")


class NoModelAvailable(RuntimeError):
    pass


class QuotaExceeded(NoModelAvailable):
    pass


@dataclass(frozen=True)
class Candidate:
    provider: ProviderConfig
    model: ModelConfig


class GatewayRouter:
    def __init__(
        self, config: GatewayConfig, quota: QuotaStore, client: httpx.AsyncClient,
        metrics: Metrics | None = None, telemetry=None,
    ):
        self.config = config
        self.quota = quota
        self.client = client
        self.metrics = metrics or Metrics()
        self.telemetry = telemetry
        self.log_provider_attempts = bool(
            config.features.structured_logging and config.observability.log_provider_attempts
        )
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._semaphores = {
            provider.name: asyncio.Semaphore(provider.max_concurrency)
            for provider in config.providers
        }

    def _available(self, provider: ProviderConfig) -> bool:
        if not self.config.features.circuit_breaker:
            return True
        return self._open_until.get(provider.name, 0) <= time.monotonic()

    async def _succeeded(self, candidate: Candidate) -> None:
        provider = candidate.provider
        if self.config.features.circuit_breaker:
            self._failures[provider.name] = 0
            self._open_until.pop(provider.name, None)
        await self.metrics.provider(
            provider.name, "success", candidate.model.name, provider.tier
        )
        await self.metrics.circuit(provider.name, False)

    async def _failed(self, candidate: Candidate) -> None:
        provider = candidate.provider
        if not self.config.features.circuit_breaker:
            await self.metrics.provider(
                provider.name, "failure", candidate.model.name, provider.tier
            )
            await self.metrics.circuit(provider.name, False)
            return
        failures = self._failures.get(provider.name, 0) + 1
        self._failures[provider.name] = failures
        if failures >= provider.failure_threshold:
            self._open_until[provider.name] = time.monotonic() + provider.cooldown_seconds
        await self.metrics.provider(
            provider.name, "failure", candidate.model.name, provider.tier
        )
        await self.metrics.circuit(
            provider.name, self._open_until.get(provider.name, 0) > time.monotonic()
        )

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [{
            "name": provider.name,
            "tier": provider.tier,
            "enabled": provider.enabled,
            "configured": bool(provider.api_key()),
            "circuit": (
                "disabled" if not self.config.features.circuit_breaker
                else "open" if self._open_until.get(provider.name, 0) > now else "closed"
            ),
            "failures": self._failures.get(provider.name, 0),
            "retry_after_seconds": max(0, round(self._open_until.get(provider.name, 0) - now)),
            "models": [model.name for model in provider.models],
            "prompt_injection_safe_models": [
                model.name for model in provider.models if model.prompt_injection_safe
            ],
        } for provider in self.config.providers]

    def candidates(
        self, requested_model: str, reasoning: bool, requested_provider: str | None = None,
        required_capabilities: set[str] | None = None,
        require_injection_safe: bool = False,
    ) -> list[Candidate]:
        result: list[Candidate] = []
        for provider in self.config.providers:
            if not provider.enabled or not provider.api_key() or not self._available(provider):
                continue
            if provider.tier == "paid" and not self.config.routing.paid_fallback:
                continue
            if requested_provider and provider.name != requested_provider:
                continue
            for model in provider.models:
                if requested_model not in {"auto", model.name}:
                    continue
                if reasoning and not model.reasoning:
                    continue
                if required_capabilities and not required_capabilities.issubset(model.capabilities):
                    continue
                if require_injection_safe and not model.prompt_injection_safe:
                    continue
                result.append(Candidate(provider, model))
        return sorted(
            result,
            key=lambda item: (item.provider.tier == "paid", item.model.priority, item.provider.name),
        )

    @staticmethod
    def estimate_tokens(payload: dict[str, Any]) -> int:
        # Conservative dependency-free estimate; reservations are reconciled with upstream usage.
        text = json.dumps(payload.get("messages", []), ensure_ascii=False)
        input_tokens = max(1, len(text) // 3)
        output_tokens = int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 1024)
        return input_tokens + output_tokens

    @staticmethod
    def wants_reasoning(payload: dict[str, Any]) -> bool:
        effort = payload.get("reasoning_effort")
        return bool(payload.get("reasoning") or (effort and effort != "none"))

    @staticmethod
    def required_capabilities(payload: dict[str, Any]) -> set[str]:
        required = {"chat"}
        if payload.get("tools"):
            required.add("tools")
        if payload.get("response_format"):
            required.add("json")
        for message in payload.get("messages", []):
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
                for part in content
            ):
                required.add("vision")
        if GatewayRouter.wants_reasoning(payload):
            required.add("reasoning")
        return required

    @staticmethod
    def upstream_error(response: httpx.Response) -> str:
        """Return a useful, bounded provider error without leaking request headers."""
        try:
            body = response.json()
            error = body.get("error", body) if isinstance(body, dict) else body
            if isinstance(error, dict):
                message = error.get("message") or error.get("detail") or str(error)
            else:
                message = str(error)
        except (ValueError, TypeError):
            message = response.text or response.reason_phrase
        return " ".join(message.split())[:500]

    async def complete(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        return await self.complete_for(payload)

    async def complete_for(
        self, payload: dict[str, Any], consumer_id: str | None = None,
        consumer_limits: tuple[Any, ...] = (),
        require_injection_safe: bool = False,
    ) -> tuple[dict[str, Any], str, str]:
        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        estimate = self.estimate_tokens(payload)
        errors: list[str] = []
        candidates = self.candidates(
            requested_model, self.wants_reasoning(payload), requested_provider,
            self.required_capabilities(payload),
            require_injection_safe,
        )
        if requested_provider and not self.config.routing.allow_provider_override:
            raise NoModelAvailable("provider override is disabled")
        max_attempts = self.config.routing.max_attempts or len(candidates)
        for candidate in candidates[:max_attempts]:
            request_id = str(uuid.uuid4())
            reservation = await self.quota.reserve(
                candidate.provider, request_id, estimate, candidate.model.name,
                candidate.model.input_cost_per_million,
                candidate.model.output_cost_per_million,
                consumer_id, consumer_limits,
            )
            if reservation is None:
                errors.append(f"{candidate.provider.name}: local quota exhausted")
                continue
            try:
                async with self._semaphores[candidate.provider.name]:
                    response, upstream_attempts = await self._post_with_retries(candidate, payload)
            except httpx.HTTPError as exc:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                if self.log_provider_attempts:
                    logger.warning(
                        "Provider request failed",
                        extra={"event_name": "provider.request", "provider": candidate.provider.name,
                               "model": candidate.model.name, "upstream_model": candidate.model.upstream_model,
                               "outcome": type(exc).__name__},
                    )
                errors.append(f"{candidate.provider.name}: {type(exc).__name__}")
                continue
            if response.is_error:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                detail = self.upstream_error(response)
                if self.log_provider_attempts:
                    logger.warning(
                        "Provider returned an error",
                        extra={"event_name": "provider.request", "provider": candidate.provider.name,
                               "model": candidate.model.name, "upstream_model": candidate.model.upstream_model,
                               "http_status_code": response.status_code,
                               "attempt": upstream_attempts, "outcome": "error"},
                    )
                if not self.config.observability.include_upstream_errors:
                    detail = "upstream request failed"
                errors.append(
                    f"{candidate.provider.name}: HTTP {response.status_code} - {detail}"
                )
                continue
            try:
                body = response.json()
            except ValueError:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                content_type = response.headers.get("content-type", "not provided")
                preview = " ".join(response.text.split())[:200] or "empty response body"
                diagnostic_headers = {
                    name: response.headers[name]
                    for name in ("server", "via", "x-github-request-id", "x-cache", "location")
                    if name in response.headers
                }
                errors.append(
                    f"{candidate.provider.name}: HTTP {response.status_code} returned "
                    f"non-JSON data ({content_type}) from {response.url} - {preview}; "
                    f"response headers={diagnostic_headers or 'none'}"
                )
                continue
            if not isinstance(body, dict):
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(
                    f"{candidate.provider.name}: HTTP {response.status_code} returned "
                    "a JSON response with an invalid top-level type"
                )
                continue
            usage = body.get("usage") or {}
            actual = usage.get("total_tokens")
            await self.quota.reconcile(
                reservation, actual, True,
                usage.get("prompt_tokens") or usage.get("input_tokens"),
                usage.get("completion_tokens") or usage.get("output_tokens"),
            )
            await self._succeeded(candidate)
            if self.log_provider_attempts:
                logger.info(
                    "Provider request completed",
                    extra={"event_name": "provider.request", "provider": candidate.provider.name,
                           "model": candidate.model.name, "upstream_model": candidate.model.upstream_model,
                           "http_status_code": response.status_code,
                           "attempt": upstream_attempts, "outcome": "success"},
                )
            body["model"] = candidate.model.name
            body["_gateway_meta"] = {
                "upstream_attempts": upstream_attempts,
                "rate_limits": {
                    key: value for key, value in response.headers.items()
                    if key.lower().startswith(("x-ratelimit", "ratelimit"))
                }
            }
            return body, candidate.provider.name, candidate.model.upstream_model
        if errors and all("quota exhausted" in error for error in errors):
            raise QuotaExceeded("; ".join(errors))
        raise NoModelAvailable("; ".join(errors) or "no configured model matches this request")

    async def stream(
        self, payload: dict[str, Any], consumer_id: str | None = None,
        consumer_limits: tuple[Any, ...] = (),
        require_injection_safe: bool = False,
    ) -> AsyncIterator[bytes]:
        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        if requested_provider and not self.config.routing.allow_provider_override:
            raise NoModelAvailable("provider override is disabled")
        estimate = self.estimate_tokens(payload)
        candidates = self.candidates(
            requested_model, self.wants_reasoning(payload), requested_provider,
            self.required_capabilities(payload),
            require_injection_safe,
        )
        max_attempts = self.config.routing.max_attempts or len(candidates)
        quota_skips = 0
        for candidate in candidates[:max_attempts]:
            reservation = await self.quota.reserve(
                candidate.provider, str(uuid.uuid4()), estimate, candidate.model.name,
                candidate.model.input_cost_per_million,
                candidate.model.output_cost_per_million,
                consumer_id, consumer_limits,
            )
            if reservation is None:
                quota_skips += 1
                continue
            attempt_started = time.monotonic()
            try:
                async with self.client.stream(
                    "POST",
                    self._url(candidate.provider),
                    headers=self._headers(candidate.provider),
                    json=self._upstream_payload(candidate, payload, stream=True),
                    timeout=candidate.provider.timeout_seconds,
                ) as response:
                    if response.status_code in FAILOVER_STATUS:
                        await response.aread()
                        await self.quota.reconcile(reservation, None, False)
                        await self.metrics.provider_request(
                            candidate.provider.name, candidate.model.name, candidate.provider.tier,
                            "error", str(response.status_code),
                            (time.monotonic() - attempt_started) * 1000,
                        )
                        await self._failed(candidate)
                        continue
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
                    # Streaming usage is provider-dependent; retain the conservative reservation.
                    await self.quota.reconcile(reservation, None, True)
                    await self.metrics.provider_request(
                        candidate.provider.name, candidate.model.name, candidate.provider.tier,
                        "success", str(response.status_code),
                        (time.monotonic() - attempt_started) * 1000,
                    )
                    await self._succeeded(candidate)
                    return
            except httpx.HTTPError as exc:
                await self.quota.reconcile(reservation, None, False)
                status = (
                    str(exc.response.status_code)
                    if isinstance(exc, httpx.HTTPStatusError) else "transport_error"
                )
                await self.metrics.provider_request(
                    candidate.provider.name, candidate.model.name, candidate.provider.tier,
                    "error", status,
                    (time.monotonic() - attempt_started) * 1000,
                )
                await self._failed(candidate)
                continue
        attempted = min(max_attempts, len(candidates))
        if attempted and quota_skips == attempted:
            raise QuotaExceeded("all eligible providers are quota exhausted")
        raise NoModelAvailable("no available model could start the stream")

    async def _post(self, candidate: Candidate, payload: dict[str, Any], stream: bool) -> httpx.Response:
        return await self.client.post(
            self._url(candidate.provider),
            headers=self._headers(candidate.provider),
            json=self._upstream_payload(candidate, payload, stream),
            timeout=candidate.provider.timeout_seconds,
        )

    async def _post_with_retries(
        self, candidate: Candidate, payload: dict[str, Any]
    ) -> tuple[httpx.Response, int]:
        routing = self.config.routing
        started = time.monotonic()
        deadline = started + routing.request_deadline_seconds if routing.request_deadline_seconds else None
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, routing.retry_attempts + 1):
            remaining = deadline - time.monotonic() if deadline else None
            if remaining is not None and remaining <= 0:
                break
            timeout = min(candidate.provider.timeout_seconds, remaining) if remaining is not None else candidate.provider.timeout_seconds
            trace_handle = (
                self.telemetry.start_provider(
                    candidate.provider.name, candidate.model.name,
                    candidate.model.upstream_model, attempt,
                )
                if self.telemetry is not None else None
            )
            attempt_started = time.monotonic()
            try:
                response = await self.client.post(
                    self._url(candidate.provider),
                    headers=self._headers(candidate.provider),
                    json=self._upstream_payload(candidate, payload, False),
                    timeout=timeout,
                )
                if trace_handle is not None:
                    self.telemetry.finish_provider(trace_handle, status_code=response.status_code)
                await self.metrics.provider_request(
                    candidate.provider.name, candidate.model.name, candidate.provider.tier,
                    "success" if response.status_code < 400 else "error",
                    str(response.status_code), (time.monotonic() - attempt_started) * 1000,
                )
                if response.status_code not in RETRY_STATUS or attempt == routing.retry_attempts:
                    return response, attempt
                delay = self._retry_delay(response, attempt)
                retry_reason = f"http_{response.status_code}"
            except httpx.HTTPError as exc:
                if trace_handle is not None:
                    self.telemetry.finish_provider(trace_handle, error_type=type(exc).__name__)
                await self.metrics.provider_request(
                    candidate.provider.name, candidate.model.name, candidate.provider.tier,
                    "error", "transport_error",
                    (time.monotonic() - attempt_started) * 1000,
                )
                last_error = exc
                if attempt == routing.retry_attempts:
                    raise
                delay = self._retry_delay(None, attempt)
                retry_reason = "transport"
            if deadline is not None and time.monotonic() + delay >= deadline:
                break
            await self.metrics.retry(
                candidate.provider.name, candidate.model.name, retry_reason
            )
            if self.log_provider_attempts:
                logger.warning(
                    "Retrying provider request",
                    extra={"event_name": "provider.retry", "provider": candidate.provider.name,
                           "model": candidate.model.name,
                           "upstream_model": candidate.model.upstream_model,
                           "attempt": attempt, "retry_reason": retry_reason,
                           "retry_delay_seconds": round(delay, 3), "outcome": "retry"},
                )
            if delay:
                await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        raise httpx.TimeoutException("gateway request deadline exhausted before another retry")

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        routing = self.config.routing
        retry_after = self._retry_after_seconds(response) if response is not None else None
        if retry_after is not None:
            return min(retry_after, routing.retry_max_backoff_seconds)
        base = min(
            routing.retry_initial_backoff_seconds * (2 ** (attempt - 1)),
            routing.retry_max_backoff_seconds,
        )
        spread = base * routing.retry_jitter_ratio
        return max(0, random.uniform(base - spread, base + spread))

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _url(provider: ProviderConfig) -> str:
        return f"{provider.base_url.rstrip('/')}/{provider.chat_completions_path.lstrip('/')}"

    @staticmethod
    def _headers(provider: ProviderConfig) -> dict[str, str]:
        credential = provider.api_key() or ""
        value = f"{provider.api_key_prefix} {credential}".strip()
        return {provider.api_key_header: value, **provider.headers}

    @staticmethod
    def _upstream_payload(candidate: Candidate, payload: dict[str, Any], stream: bool) -> dict[str, Any]:
        result = {
            **candidate.provider.request_defaults,
            **candidate.model.request_overrides,
            **payload,
        }
        result["model"] = candidate.model.upstream_model
        result["stream"] = stream
        result.pop("reasoning", None)
        result.pop("provider", None)
        for key in tuple(result):
            if key.startswith("_gateway_"):
                result.pop(key, None)
        return result


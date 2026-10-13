from __future__ import annotations

import json
import uuid
import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .config import GatewayConfig, ModelConfig, ProviderConfig
from .quota import QuotaStore, Reservation
from .metrics import Metrics


FAILOVER_STATUS = {401, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504}


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
        metrics: Metrics | None = None,
    ):
        self.config = config
        self.quota = quota
        self.client = client
        self.metrics = metrics or Metrics()
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

    async def _succeeded(self, provider: ProviderConfig) -> None:
        if self.config.features.circuit_breaker:
            self._failures[provider.name] = 0
            self._open_until.pop(provider.name, None)
        await self.metrics.provider(provider.name, "success")

    async def _failed(self, provider: ProviderConfig) -> None:
        if not self.config.features.circuit_breaker:
            await self.metrics.provider(provider.name, "failure")
            return
        failures = self._failures.get(provider.name, 0) + 1
        self._failures[provider.name] = failures
        if failures >= provider.failure_threshold:
            self._open_until[provider.name] = time.monotonic() + provider.cooldown_seconds
        await self.metrics.provider(provider.name, "failure")

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
        } for provider in self.config.providers]

    def candidates(
        self, requested_model: str, reasoning: bool, requested_provider: str | None = None,
        required_capabilities: set[str] | None = None,
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
    ) -> tuple[dict[str, Any], str, str]:
        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        estimate = self.estimate_tokens(payload)
        errors: list[str] = []
        candidates = self.candidates(
            requested_model, self.wants_reasoning(payload), requested_provider,
            self.required_capabilities(payload),
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
                    response = await self._post(candidate, payload, stream=False)
            except httpx.HTTPError as exc:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate.provider)
                errors.append(f"{candidate.provider.name}: {type(exc).__name__}")
                continue
            if response.is_error:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate.provider)
                detail = self.upstream_error(response)
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
                await self._failed(candidate.provider)
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
                await self._failed(candidate.provider)
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
            await self._succeeded(candidate.provider)
            body["model"] = candidate.model.name
            body["_gateway_meta"] = {
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
    ) -> AsyncIterator[bytes]:
        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        if requested_provider and not self.config.routing.allow_provider_override:
            raise NoModelAvailable("provider override is disabled")
        estimate = self.estimate_tokens(payload)
        candidates = self.candidates(
            requested_model, self.wants_reasoning(payload), requested_provider,
            self.required_capabilities(payload),
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
                        await self._failed(candidate.provider)
                        continue
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
                    # Streaming usage is provider-dependent; retain the conservative reservation.
                    await self.quota.reconcile(reservation, None, True)
                    await self._succeeded(candidate.provider)
                    return
            except httpx.HTTPError:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate.provider)
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
        return result


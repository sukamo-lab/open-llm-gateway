from __future__ import annotations

import json
import logging
import math
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
        self._latency_ewma_ms: dict[tuple[str, str, str], float] = {}
        self._latency_samples: dict[tuple[str, str, str], int] = {}
        self._latency_selection_count = 0
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
            "latency_observations": (
                [
                    {
                        "model": model,
                        "operation": operation,
                        "ewma_ms": round(value, 3),
                        "samples": self._latency_samples[(provider_name, model, operation)],
                    }
                    for (provider_name, model, operation), value in sorted(self._latency_ewma_ms.items())
                    if provider_name == provider.name
                ]
                if self.config.features.latency_routing else []
            ),
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
    def estimated_cost(candidate: Candidate, input_tokens: int, output_tokens: int) -> float | None:
        model = candidate.model
        known = bool(
            model.pricing_version
            or model.input_cost_per_million > 0
            or model.output_cost_per_million > 0
        )
        if not known:
            return None
        return (
            input_tokens * model.input_cost_per_million
            + output_tokens * model.output_cost_per_million
        ) / 1_000_000

    def rank_by_cost(
        self, candidates: list[Candidate], input_tokens: int, output_tokens: int,
    ) -> list[Candidate]:
        if not self.config.features.cost_routing:
            return candidates
        if self.config.routing.unknown_price_policy == "exclude":
            candidates = [
                candidate for candidate in candidates
                if self.estimated_cost(candidate, input_tokens, output_tokens) is not None
            ]
        return sorted(candidates, key=lambda candidate: (
            candidate.provider.tier == "paid",
            self.estimated_cost(candidate, input_tokens, output_tokens) is None,
            self.estimated_cost(candidate, input_tokens, output_tokens) or 0,
            candidate.model.priority,
            candidate.provider.name,
            candidate.model.upstream_model,
        ))

    @staticmethod
    def _latency_key(candidate: Candidate, operation: str) -> tuple[str, str, str]:
        return candidate.provider.name, candidate.model.name, operation

    def _observe_latency(self, candidate: Candidate, operation: str, elapsed_ms: float) -> None:
        if not self.config.features.latency_routing:
            return
        key = self._latency_key(candidate, operation)
        previous = self._latency_ewma_ms.get(key)
        alpha = self.config.routing.latency_ewma_alpha
        self._latency_ewma_ms[key] = (
            elapsed_ms if previous is None else alpha * elapsed_ms + (1 - alpha) * previous
        )
        self._latency_samples[key] = self._latency_samples.get(key, 0) + 1

    def _observe_valid_response(
        self, candidate: Candidate, operation: str, response: httpx.Response,
    ) -> None:
        elapsed_ms = response.extensions.get("gateway_elapsed_ms")
        if isinstance(elapsed_ms, (int, float)):
            self._observe_latency(candidate, operation, float(elapsed_ms))

    def latency_state(self, candidate: Candidate, operation: str) -> dict[str, Any]:
        key = self._latency_key(candidate, operation)
        samples = self._latency_samples.get(key, 0)
        observed = self._latency_ewma_ms.get(key)
        return {
            "operation": operation,
            "ewma_ms": round(observed, 3) if observed is not None else None,
            "samples": samples,
            "cold_start": samples < self.config.routing.latency_min_samples,
        }

    def rank_by_latency(self, candidates: list[Candidate], operation: str) -> list[Candidate]:
        if not self.config.features.latency_routing:
            return candidates
        self._latency_selection_count += 1
        interval = self.config.routing.latency_exploration_interval
        explore = bool(interval and self._latency_selection_count % interval == 0)

        def key(candidate: Candidate) -> tuple[Any, ...]:
            latency_key = self._latency_key(candidate, operation)
            samples = self._latency_samples.get(latency_key, 0)
            observed = self._latency_ewma_ms.get(latency_key)
            score = (
                observed
                if observed is not None and samples >= self.config.routing.latency_min_samples
                else self.config.routing.latency_cold_start_ms
            )
            return (
                candidate.provider.tier == "paid",
                samples if explore else score,
                candidate.model.priority,
                candidate.provider.name,
                candidate.model.upstream_model,
            )

        return sorted(candidates, key=key)

    @staticmethod
    def estimate_tokens(payload: dict[str, Any]) -> int:
        # Conservative dependency-free estimate; reservations are reconciled with upstream usage.
        input_tokens, output_tokens = GatewayRouter.estimate_chat_tokens(payload)
        return input_tokens + output_tokens

    @staticmethod
    def estimate_chat_tokens(payload: dict[str, Any]) -> tuple[int, int]:
        text = json.dumps(payload.get("messages", []), ensure_ascii=False)
        requested_output = payload.get("max_completion_tokens") or payload.get("max_tokens") or 1024
        if isinstance(requested_output, bool):
            requested_output = 1024
        try:
            output_tokens = max(1, min(int(requested_output), 10_000_000))
        except (TypeError, ValueError, OverflowError):
            output_tokens = 1024
        return (
            max(1, len(text) // 3),
            output_tokens,
        )

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

    async def embed_for(
        self, payload: dict[str, Any], consumer_id: str | None = None,
        consumer_limits: tuple[Any, ...] = (),
    ) -> tuple[dict[str, Any], str, str]:
        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        if requested_provider and not self.config.routing.allow_provider_override:
            raise NoModelAvailable("provider override is disabled")
        candidates = self.candidates(
            requested_model, False, requested_provider, {"embeddings"}
        )
        estimate = self.estimate_embedding_tokens(payload.get("input"))
        candidates = self.rank_by_cost(candidates, estimate, 0)
        candidates = self.rank_by_latency(candidates, "embeddings")
        errors: list[str] = []
        max_attempts = self.config.routing.max_attempts or len(candidates)
        for candidate in candidates[:max_attempts]:
            reservation = await self.quota.reserve(
                candidate.provider, str(uuid.uuid4()), estimate, candidate.model.name,
                candidate.model.input_cost_per_million, 0,
                consumer_id, consumer_limits,
            )
            if reservation is None:
                errors.append(f"{candidate.provider.name}: local quota exhausted")
                continue
            try:
                async with self._semaphores[candidate.provider.name]:
                    response, upstream_attempts = await self._post_with_retries(
                        candidate, payload, "embeddings"
                    )
            except httpx.HTTPError as exc:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                if self.log_provider_attempts:
                    logger.warning(
                        "Embeddings provider request failed",
                        extra={"event_name": "provider.request", "operation": "embeddings",
                               "provider": candidate.provider.name, "model": candidate.model.name,
                               "outcome": type(exc).__name__},
                    )
                errors.append(f"{candidate.provider.name}: {type(exc).__name__}")
                continue
            if response.is_error:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                detail = self.upstream_error(response)
                if not self.config.observability.include_upstream_errors:
                    detail = "upstream request failed"
                if self.log_provider_attempts:
                    logger.warning(
                        "Embeddings provider returned an error",
                        extra={"event_name": "provider.request", "operation": "embeddings",
                               "provider": candidate.provider.name, "model": candidate.model.name,
                               "http_status_code": response.status_code, "outcome": "error"},
                    )
                errors.append(f"{candidate.provider.name}: HTTP {response.status_code} - {detail}")
                continue
            try:
                body = response.json()
            except ValueError:
                body = None
            data = body.get("data") if isinstance(body, dict) else None
            valid_data = isinstance(data, list) and bool(data) and all(
                isinstance(item, dict)
                and type(item.get("index")) is int
                and item["index"] >= 0
                and isinstance(item.get("embedding"), (list, str))
                and (
                    isinstance(item.get("embedding"), str) and bool(item["embedding"])
                    or (
                        bool(item["embedding"])
                        and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(float(value))
                                for value in item["embedding"])
                    )
                )
                for item in (data or [])
            )
            if valid_data:
                encoding = payload.get("encoding_format", "float")
                valid_data = all(
                    isinstance(item["embedding"], str)
                    if encoding == "base64" else isinstance(item["embedding"], list)
                    for item in data
                )
            if valid_data and payload.get("dimensions") is not None:
                valid_data = all(
                    isinstance(item["embedding"], list)
                    and len(item["embedding"]) == payload["dimensions"]
                    for item in data
                )
            if valid_data and payload.get("encoding_format", "float") == "float":
                valid_data = len({len(item["embedding"]) for item in data}) == 1
            if valid_data:
                input_value = payload.get("input")
                if isinstance(input_value, str):
                    expected_items = 1
                elif (
                    isinstance(input_value, list) and input_value
                    and all(type(value) is int for value in input_value)
                ):
                    expected_items = 1
                else:
                    expected_items = len(input_value) if isinstance(input_value, list) else 0
                valid_data = {item["index"] for item in data} == set(range(expected_items))
            if not isinstance(body, dict) or not valid_data:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: upstream returned an invalid embeddings response")
                continue
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            try:
                prompt_tokens = (
                    int(usage.get("prompt_tokens") or usage.get("input_tokens"))
                    if (
                        usage.get("prompt_tokens") is not None
                        or usage.get("input_tokens") is not None
                    )
                    else None
                )
                actual = (
                    int(usage["total_tokens"])
                    if usage.get("total_tokens") is not None else prompt_tokens
                )
                if (
                    prompt_tokens is not None and prompt_tokens < 0
                    or actual is not None and actual < 0
                ):
                    raise ValueError
            except (TypeError, ValueError):
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: upstream returned invalid embeddings usage")
                continue
            await self.quota.reconcile(
                reservation, actual, True, prompt_tokens, 0,
            )
            self._observe_valid_response(candidate, "embeddings", response)
            await self._succeeded(candidate)
            if self.log_provider_attempts:
                logger.info(
                    "Embeddings provider request completed",
                    extra={"event_name": "provider.request", "operation": "embeddings",
                           "provider": candidate.provider.name, "model": candidate.model.name,
                           "http_status_code": response.status_code,
                           "attempt": upstream_attempts, "outcome": "success"},
                )
            body["object"] = "list"
            body["data"] = sorted(data, key=lambda item: item["index"])
            for item in body["data"]:
                item["object"] = "embedding"
            reported_tokens = actual if actual is not None else estimate
            body["usage"] = {
                **usage,
                "prompt_tokens": (
                    prompt_tokens if prompt_tokens is not None else reported_tokens
                ),
                "total_tokens": reported_tokens,
            }
            body["model"] = candidate.model.name
            body["gateway"] = {
                "provider": candidate.provider.name,
                "upstream_model": candidate.model.upstream_model,
                "upstream_attempts": upstream_attempts,
                "rate_limits": {
                    key: value for key, value in response.headers.items()
                    if key.lower().startswith(("x-ratelimit", "ratelimit"))
                },
            }
            return body, candidate.provider.name, candidate.model.upstream_model
        if errors and all("quota exhausted" in error for error in errors):
            raise QuotaExceeded("; ".join(errors))
        raise NoModelAvailable("; ".join(errors) or "no configured embeddings model matches this request")

    async def speech_for(
        self, payload: dict[str, Any], consumer_id: str | None = None,
        consumer_limits: tuple[Any, ...] = (),
    ) -> tuple[bytes, str, str, httpx.Headers]:
        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        if requested_provider and not self.config.routing.allow_provider_override:
            raise NoModelAvailable("provider override is disabled")
        candidates = self.candidates(
            requested_model, False, requested_provider, {"audio_speech"}
        )
        estimate = max(1, len(str(payload.get("input", ""))) // 3)
        candidates = self.rank_by_cost(candidates, estimate, 0)
        candidates = self.rank_by_latency(candidates, "audio_speech")
        errors: list[str] = []
        max_attempts = self.config.routing.max_attempts or len(candidates)
        for candidate in candidates[:max_attempts]:
            reservation = await self.quota.reserve(
                candidate.provider, str(uuid.uuid4()), estimate, candidate.model.name,
                candidate.model.input_cost_per_million, 0, consumer_id, consumer_limits,
            )
            if reservation is None:
                errors.append(f"{candidate.provider.name}: local quota exhausted")
                continue
            try:
                async with self._semaphores[candidate.provider.name]:
                    response, _attempts = await self._post_with_retries(
                        candidate, payload, "audio_speech"
                    )
            except httpx.HTTPError as exc:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: {type(exc).__name__}")
                continue
            if response.is_error:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                detail = self.upstream_error(response)
                if not self.config.observability.include_upstream_errors:
                    detail = "upstream request failed"
                errors.append(f"{candidate.provider.name}: HTTP {response.status_code} - {detail}")
                continue
            content_type = response.headers.get("content-type", "").lower()
            if not response.content or content_type.startswith(("application/json", "text/")):
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: upstream returned invalid audio")
                continue
            if len(response.content) > self.config.audio.max_response_bytes:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: upstream audio exceeds configured limit")
                continue
            await self.quota.reconcile(reservation, estimate, True, estimate, 0)
            self._observe_valid_response(candidate, "audio_speech", response)
            await self._succeeded(candidate)
            return response.content, candidate.provider.name, candidate.model.upstream_model, response.headers
        if errors and all("quota exhausted" in error for error in errors):
            raise QuotaExceeded("; ".join(errors))
        raise NoModelAvailable("; ".join(errors) or "no configured speech model matches this request")

    async def generate_images_for(
        self, payload: dict[str, Any], consumer_id: str | None = None,
        consumer_limits: tuple[Any, ...] = (),
    ) -> tuple[dict[str, Any], str, str]:
        from .images_api import validate_image_response

        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        if requested_provider and not self.config.routing.allow_provider_override:
            raise NoModelAvailable("provider override is disabled")
        candidates = self.candidates(
            requested_model, False, requested_provider, {"image_generation"}
        )
        estimate = max(1, len(str(payload.get("prompt", ""))) // 3)
        candidates = self.rank_by_cost(candidates, estimate, 0)
        candidates = self.rank_by_latency(candidates, "image_generation")
        errors: list[str] = []
        max_attempts = self.config.routing.max_attempts or len(candidates)
        for candidate in candidates[:max_attempts]:
            reservation = await self.quota.reserve(
                candidate.provider, str(uuid.uuid4()), estimate, candidate.model.name,
                candidate.model.input_cost_per_million, candidate.model.output_cost_per_million,
                consumer_id, consumer_limits,
            )
            if reservation is None:
                errors.append(f"{candidate.provider.name}: local quota exhausted")
                continue
            try:
                async with self._semaphores[candidate.provider.name]:
                    response, attempts = await self._post_with_retries(
                        candidate, payload, "image_generation"
                    )
            except httpx.HTTPError as exc:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: {type(exc).__name__}")
                continue
            if response.is_error:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                detail = self.upstream_error(response)
                if not self.config.observability.include_upstream_errors:
                    detail = "upstream request failed"
                errors.append(f"{candidate.provider.name}: HTTP {response.status_code} - {detail}")
                continue
            if len(response.content) > self.config.images.max_response_bytes:
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: upstream image response exceeds configured limit")
                continue
            try:
                body = response.json()
            except ValueError:
                body = None
            if not validate_image_response(body, payload["n"], payload["response_format"]):
                await self.quota.reconcile(reservation, None, False)
                await self._failed(candidate)
                errors.append(f"{candidate.provider.name}: upstream returned invalid image data")
                continue
            await self.quota.reconcile(reservation, estimate, True, estimate, 0)
            self._observe_valid_response(candidate, "image_generation", response)
            await self._succeeded(candidate)
            body["gateway"] = {
                "provider": candidate.provider.name,
                "upstream_model": candidate.model.upstream_model,
                "upstream_attempts": attempts,
                "rate_limits": {
                    key: value for key, value in response.headers.items()
                    if key.lower().startswith(("x-ratelimit", "ratelimit"))
                },
            }
            return body, candidate.provider.name, candidate.model.upstream_model
        if errors and all("quota exhausted" in error for error in errors):
            raise QuotaExceeded("; ".join(errors))
        raise NoModelAvailable("; ".join(errors) or "no configured image model matches this request")

    @staticmethod
    def estimate_embedding_tokens(input_value: Any) -> int:
        if isinstance(input_value, str):
            return max(1, len(input_value) // 3)
        if isinstance(input_value, list):
            if input_value and all(isinstance(item, int) for item in input_value):
                return max(1, len(input_value))
            return max(1, sum(GatewayRouter.estimate_embedding_tokens(item) for item in input_value))
        return 1

    async def complete_for(
        self, payload: dict[str, Any], consumer_id: str | None = None,
        consumer_limits: tuple[Any, ...] = (),
        require_injection_safe: bool = False,
    ) -> tuple[dict[str, Any], str, str]:
        requested_model = str(payload.get("model", "auto"))
        requested_provider = payload.get("provider")
        estimate = self.estimate_tokens(payload)
        estimated_input, estimated_output = self.estimate_chat_tokens(payload)
        errors: list[str] = []
        candidates = self.candidates(
            requested_model, self.wants_reasoning(payload), requested_provider,
            self.required_capabilities(payload),
            require_injection_safe,
        )
        candidates = self.rank_by_cost(candidates, estimated_input, estimated_output)
        candidates = self.rank_by_latency(candidates, "chat")
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
            self._observe_valid_response(candidate, "chat", response)
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
                "routing": {
                    "strategy": (
                        "least_cost" if self.config.features.cost_routing
                        else "latency" if self.config.features.latency_routing
                        else "priority"
                    ),
                    "estimated_cost_usd": self.estimated_cost(
                        candidate, estimated_input, estimated_output
                    ),
                    "pricing_version": candidate.model.pricing_version,
                    **(
                        {"latency": self.latency_state(candidate, "chat")}
                        if self.config.features.latency_routing else {}
                    ),
                },
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
        estimated_input, estimated_output = self.estimate_chat_tokens(payload)
        candidates = self.candidates(
            requested_model, self.wants_reasoning(payload), requested_provider,
            self.required_capabilities(payload),
            require_injection_safe,
        )
        candidates = self.rank_by_cost(candidates, estimated_input, estimated_output)
        candidates = self.rank_by_latency(candidates, "chat")
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
                    self._observe_latency(
                        candidate, "chat", (time.monotonic() - attempt_started) * 1000
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
        self, candidate: Candidate, payload: dict[str, Any], operation: str = "chat"
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
                    candidate.model.upstream_model, attempt, operation,
                )
                if self.telemetry is not None else None
            )
            attempt_started = time.monotonic()
            try:
                response = await self.client.post(
                    self._operation_url(candidate.provider, operation),
                    headers=self._headers(candidate.provider),
                    json=(
                        self._operation_payload(candidate, payload, operation)
                    ),
                    timeout=timeout,
                )
                if trace_handle is not None:
                    self.telemetry.finish_provider(trace_handle, status_code=response.status_code)
                elapsed_ms = (time.monotonic() - attempt_started) * 1000
                response.extensions["gateway_elapsed_ms"] = elapsed_ms
                await self.metrics.provider_request(
                    candidate.provider.name, candidate.model.name, candidate.provider.tier,
                    "success" if response.status_code < 400 else "error",
                    str(response.status_code), elapsed_ms,
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
    def _embeddings_url(provider: ProviderConfig) -> str:
        return f"{provider.base_url.rstrip('/')}/{provider.embeddings_path.lstrip('/')}"

    @staticmethod
    def _operation_url(provider: ProviderConfig, operation: str) -> str:
        if operation == "embeddings":
            path = provider.embeddings_path
        elif operation == "audio_speech":
            path = provider.audio_speech_path
        elif operation == "image_generation":
            path = provider.image_generations_path
        else:
            path = provider.chat_completions_path
        return f"{provider.base_url.rstrip('/')}/{path.lstrip('/')}"

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

    @staticmethod
    def _embedding_payload(candidate: Candidate, payload: dict[str, Any]) -> dict[str, Any]:
        result = {
            **candidate.provider.embeddings_request_defaults,
            **candidate.model.request_overrides,
            **payload,
            "model": candidate.model.upstream_model,
        }
        result.pop("provider", None)
        for key in tuple(result):
            if key.startswith("_gateway_"):
                result.pop(key, None)
        return result

    @staticmethod
    def _operation_payload(
        candidate: Candidate, payload: dict[str, Any], operation: str,
    ) -> dict[str, Any]:
        if operation == "embeddings":
            return GatewayRouter._embedding_payload(candidate, payload)
        if operation == "audio_speech":
            result = {
                **candidate.provider.audio_speech_request_defaults,
                **candidate.model.request_overrides,
                **payload,
                "model": candidate.model.upstream_model,
            }
            result.pop("provider", None)
            for key in tuple(result):
                if key.startswith("_gateway_"):
                    result.pop(key, None)
            return result
        if operation == "image_generation":
            result = {
                **candidate.provider.image_request_defaults,
                **candidate.model.request_overrides,
                **payload,
                "model": candidate.model.upstream_model,
            }
            result.pop("provider", None)
            for key in tuple(result):
                if key.startswith("_gateway_"):
                    result.pop(key, None)
            return result
        return GatewayRouter._upstream_payload(candidate, payload, False)


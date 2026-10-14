from __future__ import annotations

import asyncio
from collections import Counter
from time import monotonic


DEFAULT_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
DEFAULT_PROVIDER_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace('"', '\\"')


def _labels(names: tuple[str, ...], values: tuple[object, ...], extra: str = "") -> str:
    result = [f'{name}="{_escape(value)}"' for name, value in zip(names, values)]
    if extra:
        result.append(extra)
    return "{" + ",".join(result) + "}"


class Histogram:
    def __init__(self, buckets: tuple[float, ...]):
        self.buckets = buckets
        self.counts: Counter[tuple[object, ...]] = Counter()
        self.sums: Counter[tuple[object, ...]] = Counter()
        self.bucket_counts: Counter[tuple[tuple[object, ...], float]] = Counter()

    def observe(self, labels: tuple[object, ...], value: float) -> None:
        self.counts[labels] += 1
        self.sums[labels] += value
        for bucket in self.buckets:
            if value <= bucket:
                self.bucket_counts[(labels, bucket)] += 1

    def render(self, name: str, label_names: tuple[str, ...]) -> list[str]:
        lines: list[str] = []
        for labels in sorted(self.counts, key=lambda item: tuple(map(str, item))):
            for bucket in self.buckets:
                bucket_labels = _labels(label_names, labels, f'le="{bucket:g}"')
                lines.append(
                    f'{name}_bucket{bucket_labels} {self.bucket_counts[(labels, bucket)]}'
                )
            infinite_labels = _labels(label_names, labels, 'le="+Inf"')
            lines.append(f'{name}_bucket{infinite_labels} {self.counts[labels]}')
            lines.append(f'{name}_sum{_labels(label_names, labels)} {self.sums[labels]:.9f}')
            lines.append(f'{name}_count{_labels(label_names, labels)} {self.counts[labels]}')
        return lines


class Metrics:
    def __init__(
        self, http_buckets: tuple[float, ...] = DEFAULT_HTTP_BUCKETS,
        provider_buckets: tuple[float, ...] = DEFAULT_PROVIDER_BUCKETS,
    ) -> None:
        self.started = monotonic()
        self._lock = asyncio.Lock()
        self.requests: Counter[tuple[str, str]] = Counter()
        self.provider_attempts: Counter[tuple[str, str]] = Counter()
        self.provider_results: Counter[tuple[str, str, str, str]] = Counter()
        self.provider_requests: Counter[tuple[str, str, str, str, str]] = Counter()
        self.cache_requests: Counter[tuple[str, str]] = Counter()
        self.retries: Counter[tuple[str, str, str]] = Counter()
        self.pii_events: Counter[tuple[str, str, str]] = Counter()
        self.moderation_events: Counter[tuple[str, str, str]] = Counter()
        self.injection_events: Counter[tuple[str, str]] = Counter()
        self.circuits: dict[str, int] = {}
        self.latency_ms: Counter[str] = Counter()
        self.http_duration = Histogram(http_buckets)
        self.provider_duration = Histogram(provider_buckets)

    async def request(self, endpoint: str, status: int, elapsed_ms: float) -> None:
        async with self._lock:
            self.requests[(endpoint, str(status))] += 1
            self.latency_ms[endpoint] += int(elapsed_ms)
            self.http_duration.observe((endpoint,), elapsed_ms / 1000)

    async def provider(
        self, provider: str, outcome: str, model: str = "unknown", tier: str = "unknown"
    ) -> None:
        async with self._lock:
            self.provider_attempts[(provider, outcome)] += 1
            self.provider_results[(provider, model, tier, outcome)] += 1

    async def provider_request(
        self, provider: str, model: str, tier: str, outcome: str,
        status: str, elapsed_ms: float,
    ) -> None:
        async with self._lock:
            self.provider_requests[(provider, model, tier, outcome, status)] += 1
            self.provider_duration.observe((provider, model, tier), elapsed_ms / 1000)

    async def retry(self, provider: str, model: str, reason: str) -> None:
        async with self._lock:
            self.retries[(provider, model, reason)] += 1

    async def cache(self, backend: str, outcome: str) -> None:
        async with self._lock:
            self.cache_requests[(backend, outcome)] += 1

    async def circuit(self, provider: str, is_open: bool) -> None:
        async with self._lock:
            self.circuits[provider] = int(is_open)

    async def pii(self, stage: str, action: str, outcome: str) -> None:
        async with self._lock:
            self.pii_events[(stage, action, outcome)] += 1

    async def moderation(self, stage: str, action: str, outcome: str) -> None:
        async with self._lock:
            self.moderation_events[(stage, action, outcome)] += 1

    async def prompt_injection(self, action: str, outcome: str) -> None:
        async with self._lock:
            self.injection_events[(action, outcome)] += 1

    async def render(self) -> str:
        async with self._lock:
            lines = [
                "# HELP llm_gateway_uptime_seconds Process uptime.",
                "# TYPE llm_gateway_uptime_seconds gauge",
                f"llm_gateway_uptime_seconds {monotonic() - self.started:.3f}",
                "# HELP llm_gateway_http_requests_total HTTP requests.",
                "# TYPE llm_gateway_http_requests_total counter",
            ]
            lines.extend(
                f'llm_gateway_http_requests_total{_labels(("endpoint", "status"), key)} {count}'
                for key, count in sorted(self.requests.items())
            )
            lines += [
                "# HELP llm_gateway_provider_attempts_total Validated provider candidate outcomes.",
                "# TYPE llm_gateway_provider_attempts_total counter",
            ]
            lines.extend(
                f'llm_gateway_provider_attempts_total{_labels(("provider", "outcome"), key)} {count}'
                for key, count in sorted(self.provider_attempts.items())
            )
            lines += [
                "# HELP llm_gateway_provider_results_total Validated provider/model outcomes.",
                "# TYPE llm_gateway_provider_results_total counter",
            ]
            lines.extend(
                f'llm_gateway_provider_results_total{_labels(("provider", "model", "tier", "outcome"), key)} {count}'
                for key, count in sorted(self.provider_results.items())
            )
            lines += [
                "# HELP llm_gateway_provider_requests_total Raw upstream HTTP attempts.",
                "# TYPE llm_gateway_provider_requests_total counter",
            ]
            lines.extend(
                f'llm_gateway_provider_requests_total{_labels(("provider", "model", "tier", "outcome", "status"), key)} {count}'
                for key, count in sorted(self.provider_requests.items())
            )
            lines += [
                "# HELP llm_gateway_provider_retries_total Same-provider retries.",
                "# TYPE llm_gateway_provider_retries_total counter",
            ]
            lines.extend(
                f'llm_gateway_provider_retries_total{_labels(("provider", "model", "reason"), key)} {count}'
                for key, count in sorted(self.retries.items())
            )
            lines += [
                "# HELP llm_gateway_cache_requests_total Exact-cache outcomes.",
                "# TYPE llm_gateway_cache_requests_total counter",
            ]
            lines.extend(
                f'llm_gateway_cache_requests_total{_labels(("backend", "outcome"), key)} {count}'
                for key, count in sorted(self.cache_requests.items())
            )
            lines += [
                "# HELP llm_gateway_prompt_injection_events_total Prompt-injection risk evaluations without content or identity labels.",
                "# TYPE llm_gateway_prompt_injection_events_total counter",
            ]
            lines.extend(
                f'llm_gateway_prompt_injection_events_total{_labels(("action", "outcome"), key)} {count}'
                for key, count in sorted(self.injection_events.items())
            )
            lines += [
                "# HELP llm_gateway_moderation_events_total Moderation evaluations without content or identity labels.",
                "# TYPE llm_gateway_moderation_events_total counter",
            ]
            lines.extend(
                f'llm_gateway_moderation_events_total{_labels(("stage", "action", "outcome"), key)} {count}'
                for key, count in sorted(self.moderation_events.items())
            )
            lines += [
                "# HELP llm_gateway_pii_events_total PII policy evaluations without content or identity labels.",
                "# TYPE llm_gateway_pii_events_total counter",
            ]
            lines.extend(
                f'llm_gateway_pii_events_total{_labels(("stage", "action", "outcome"), key)} {count}'
                for key, count in sorted(self.pii_events.items())
            )
            lines += [
                "# HELP llm_gateway_provider_circuit_open Whether the provider circuit is open.",
                "# TYPE llm_gateway_provider_circuit_open gauge",
            ]
            lines.extend(
                f'llm_gateway_provider_circuit_open{_labels(("provider",), (provider,))} {state}'
                for provider, state in sorted(self.circuits.items())
            )
            lines += [
                "# HELP llm_gateway_http_request_duration_milliseconds_sum Total HTTP request time.",
                "# TYPE llm_gateway_http_request_duration_milliseconds_sum counter",
            ]
            lines.extend(
                f'llm_gateway_http_request_duration_milliseconds_sum{_labels(("endpoint",), (endpoint,))} {total}'
                for endpoint, total in sorted(self.latency_ms.items())
            )
            lines += [
                "# HELP llm_gateway_http_request_duration_seconds Gateway request latency.",
                "# TYPE llm_gateway_http_request_duration_seconds histogram",
                *self.http_duration.render("llm_gateway_http_request_duration_seconds", ("endpoint",)),
                "# HELP llm_gateway_provider_request_duration_seconds Raw upstream attempt latency.",
                "# TYPE llm_gateway_provider_request_duration_seconds histogram",
                *self.provider_duration.render(
                    "llm_gateway_provider_request_duration_seconds", ("provider", "model", "tier")
                ),
            ]
            return "\n".join(lines) + "\n"


class NullMetrics:
    """Zero-work metrics implementation used when instrumentation is disabled."""

    async def request(self, endpoint: str, status: int, elapsed_ms: float) -> None:
        return None

    async def provider(
        self, provider: str, outcome: str, model: str = "unknown", tier: str = "unknown"
    ) -> None:
        return None

    async def provider_request(
        self, provider: str, model: str, tier: str, outcome: str,
        status: str, elapsed_ms: float,
    ) -> None:
        return None

    async def retry(self, provider: str, model: str, reason: str) -> None:
        return None

    async def cache(self, backend: str, outcome: str) -> None:
        return None

    async def circuit(self, provider: str, is_open: bool) -> None:
        return None

    async def pii(self, stage: str, action: str, outcome: str) -> None:
        return None

    async def moderation(self, stage: str, action: str, outcome: str) -> None:
        return None

    async def prompt_injection(self, action: str, outcome: str) -> None:
        return None

    async def render(self) -> str:
        return ""

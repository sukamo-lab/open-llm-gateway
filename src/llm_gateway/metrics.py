from __future__ import annotations

import asyncio
from collections import Counter
from time import monotonic


class Metrics:
    def __init__(self) -> None:
        self.started = monotonic()
        self._lock = asyncio.Lock()
        self.requests: Counter[tuple[str, str]] = Counter()
        self.provider_attempts: Counter[tuple[str, str]] = Counter()
        self.latency_ms: Counter[str] = Counter()

    async def request(self, endpoint: str, status: int, elapsed_ms: float) -> None:
        async with self._lock:
            self.requests[(endpoint, str(status))] += 1
            self.latency_ms[endpoint] += int(elapsed_ms)

    async def provider(self, provider: str, outcome: str) -> None:
        async with self._lock:
            self.provider_attempts[(provider, outcome)] += 1

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
                f'llm_gateway_http_requests_total{{endpoint="{ep}",status="{status}"}} {count}'
                for (ep, status), count in sorted(self.requests.items())
            )
            lines += [
                "# HELP llm_gateway_provider_attempts_total Provider attempts.",
                "# TYPE llm_gateway_provider_attempts_total counter",
            ]
            lines.extend(
                f'llm_gateway_provider_attempts_total{{provider="{provider}",outcome="{outcome}"}} {count}'
                for (provider, outcome), count in sorted(self.provider_attempts.items())
            )
            lines += [
                "# HELP llm_gateway_http_request_duration_milliseconds_sum Total HTTP request time.",
                "# TYPE llm_gateway_http_request_duration_milliseconds_sum counter",
            ]
            lines.extend(
                f'llm_gateway_http_request_duration_milliseconds_sum{{endpoint="{endpoint}"}} {total}'
                for endpoint, total in sorted(self.latency_ms.items())
            )
            return "\n".join(lines) + "\n"


class NullMetrics:
    """Zero-work metrics implementation used when instrumentation is disabled."""

    async def request(self, endpoint: str, status: int, elapsed_ms: float) -> None:
        return None

    async def provider(self, provider: str, outcome: str) -> None:
        return None

    async def render(self) -> str:
        return ""

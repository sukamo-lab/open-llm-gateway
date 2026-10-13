"""Small dependency-free (beyond project deps) gateway latency/load probe."""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time

import httpx


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--chat", action="store_true", help="Call chat instead of the local health probe")
    parser.add_argument("--model", default="fast")
    args = parser.parse_args()
    semaphore = asyncio.Semaphore(args.concurrency)
    latencies: list[float] = []
    failures = 0
    headers = {}
    if key := os.getenv("LLM_GATEWAY_BENCHMARK_KEY"):
        headers["Authorization"] = f"Bearer {key}"

    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=180) as client:
        async def one() -> None:
            nonlocal failures
            async with semaphore:
                started = time.perf_counter()
                if args.chat:
                    response = await client.post("/v1/chat/completions", json={
                        "model": args.model,
                        "messages": [{"role": "user", "content": "Reply with OK"}],
                        "max_tokens": 8,
                    })
                else:
                    response = await client.get("/health")
                latencies.append((time.perf_counter() - started) * 1000)
                failures += int(response.is_error)

        started = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(args.requests)))
        elapsed = time.perf_counter() - started

    print(f"requests={args.requests} failures={failures} concurrency={args.concurrency}")
    print(f"throughput={args.requests / elapsed:.2f} req/s")
    print(
        f"latency_ms mean={statistics.mean(latencies):.2f} "
        f"p50={percentile(latencies, .50):.2f} p95={percentile(latencies, .95):.2f} "
        f"p99={percentile(latencies, .99):.2f}"
    )


if __name__ == "__main__":
    asyncio.run(main())

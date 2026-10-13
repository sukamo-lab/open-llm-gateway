from __future__ import annotations

import os
import uuid

import pytest

from llm_gateway.config import Limit, ProviderConfig
from llm_gateway.postgres import PostgresStore


pytestmark = pytest.mark.integration


def postgres_dsn() -> str:
    dsn = os.getenv("LLM_GATEWAY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LLM_GATEWAY_TEST_POSTGRES_DSN is not configured")
    return dsn


def provider(name: str) -> ProviderConfig:
    return ProviderConfig.model_validate(
        {
            "name": name,
            "base_url": "https://example.test/v1",
            "api_key_value": "integration-test-key",
            "limits": [{"requests": 1, "tokens": 10, "window_seconds": 60}],
            "models": [{"name": "chat", "upstream_model": "model"}],
        }
    )


@pytest.mark.asyncio
async def test_postgres_quota_reservation_reconciliation_and_snapshot():
    store = PostgresStore(postgres_dsn(), identity_enabled=False)
    await store.initialize()
    suffix = uuid.uuid4().hex
    configured_provider = provider(f"provider-{suffix}")
    try:
        first = await store.reserve(configured_provider, f"request-{suffix}-1", 8)
        assert first is not None
        assert await store.reserve(configured_provider, f"request-{suffix}-2", 1) is None

        await store.reconcile(first, None, False)
        second = await store.reserve(configured_provider, f"request-{suffix}-2", 10)
        assert second is not None
        await store.reconcile(second, 7, True, prompt_tokens=5, completion_tokens=2)

        row = next(item for item in await store.snapshot() if item["provider"] == configured_provider.name)
        assert row["requests"] == 1
        assert row["tokens"] == 7
        assert row["prompt_tokens"] == 5
        assert row["completion_tokens"] == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_tenant_key_lifecycle_and_limits():
    store = PostgresStore(postgres_dsn(), cache_seconds=0, provider_quotas=False)
    await store.initialize()
    suffix = uuid.uuid4().hex
    try:
        tenant = await store.create_tenant(f"tenant-{suffix}", requests_per_minute=3)
        issued = await store.issue_key(tenant["id"], "integration-key", expires_in_days=1)

        context = await store.authenticate(issued["api_key"])
        assert context is not None
        assert context.tenant_id == tenant["id"]
        assert context.limits == (Limit(requests=3, window_seconds=60),)

        assert await store.revoke_key(issued["id"])
        assert await store.authenticate(issued["api_key"]) is None
    finally:
        await store.close()

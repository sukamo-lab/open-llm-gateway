from __future__ import annotations

import os
import uuid

import pytest

from llm_gateway.config import Limit, ProviderConfig
from llm_gateway.cache import PostgresCacheStore
from llm_gateway.idempotency import PostgresIdempotencyStore, key_hash, request_hash
from llm_gateway.migrations import LATEST_VERSIONS
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
        assert (await store.migration_status())["usage"] == LATEST_VERSIONS["usage"]
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
        assert await store.migration_status() == {
            component: LATEST_VERSIONS[component] for component in ("usage", "identity")
        }
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


@pytest.mark.asyncio
async def test_postgres_idempotency_claim_and_replay():
    store = PostgresIdempotencyStore(postgres_dsn(), 3600, 60)
    await store.initialize()
    suffix = uuid.uuid4().hex
    digest = key_hash(f"key-{suffix}")
    fingerprint = request_hash({"model": "chat", "messages": []})
    try:
        claim = await store.claim(f"tenant-{suffix}", digest, fingerprint)
        assert claim.status == "owner"
        assert await store.complete(
            f"tenant-{suffix}", digest, claim.owner_token, '{"id":"cached"}'
        )
        replay = await store.claim(f"tenant-{suffix}", digest, fingerprint)
        assert replay.status == "cached"
        assert replay.response == {"id": "cached"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_exact_cache_is_tenant_scoped():
    store = PostgresCacheStore(postgres_dsn(), ttl_seconds=60, max_entries=100)
    await store.initialize()
    suffix = uuid.uuid4().hex
    try:
        await store.put(f"tenant-a-{suffix}", suffix, '{"id":"cached"}')
        entry = await store.get(f"tenant-a-{suffix}", suffix)
        assert entry and entry.response == {"id": "cached"}
        assert await store.get(f"tenant-b-{suffix}", suffix) is None
    finally:
        await store.close()

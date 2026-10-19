from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid

import pytest

from llm_gateway.config import Limit, PlanConfig, ProviderConfig
from llm_gateway.cache import PostgresCacheStore
from llm_gateway.idempotency import PostgresIdempotencyStore, key_hash, request_hash
from llm_gateway.jobs import PostgresJobStore
from llm_gateway.migrations import LATEST_VERSIONS
from llm_gateway.postgres import PostgresStore
from llm_gateway.coordination import PostgresCoordinator


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
    plan = PlanConfig(
        id="integration-plan", pricing_version="2026-10-19",
        entitlements={"chat"}, requests_per_minute=2,
    )
    store = PostgresStore(
        postgres_dsn(), cache_seconds=0, provider_quotas=False,
        plans={plan.id: plan},
    )
    await store.initialize()
    suffix = uuid.uuid4().hex
    try:
        assert await store.migration_status() == {
            component: LATEST_VERSIONS[component] for component in ("usage", "identity")
        }
        tenant = await store.create_tenant(
            f"tenant-{suffix}", requests_per_minute=3, plan_id=plan.id,
        )
        issued = await store.issue_key(tenant["id"], "integration-key", expires_in_days=1)

        context = await store.authenticate(issued["api_key"])
        assert context is not None
        assert context.tenant_id == tenant["id"]
        assert context.limits == (
            Limit(requests=3, window_seconds=60),
            Limit(requests=2, window_seconds=60),
        )
        assert context.plan_id == plan.id
        assert context.entitlements == frozenset({"chat"})

        grant = await store.grant_credit(
            tenant["id"], 12.5, "integration", time.time() + 3600,
        )
        assert await store.list_credits(tenant["id"]) == [grant]
        with pytest.raises(Exception, match="append-only"):
            await store.pool.execute(
                "UPDATE tenant_credit_grants SET amount_usd=1 WHERE id=$1", grant["id"],
            )

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


@pytest.mark.asyncio
async def test_postgres_usage_export_and_checkpoint_round_trip():
    store = PostgresStore(postgres_dsn(), identity_enabled=False)
    await store.initialize()
    suffix = uuid.uuid4().hex
    configured_providers = [provider(f"export-provider-{suffix}-{index}") for index in range(2)]
    request_ids = [f"export-{suffix}-{index}" for index in range(2)]
    sink_id = f"sink-{suffix}"
    try:
        for index, request_id in enumerate(request_ids):
            reservation = await store.reserve(configured_providers[index], request_id, 5)
            assert reservation is not None
            await store.reconcile(
                reservation, index + 2, True,
                prompt_tokens=index + 1, completion_tokens=1,
            )

        exported = [
            row for row in await store.usage_export(limit=10_000)
            if row["request_id"] in request_ids
        ]
        assert [row["request_id"] for row in exported] == request_ids
        last = exported[-1]
        await store.save_export_checkpoint(
            sink_id, float(last["created_at"]), str(last["request_id"]), "a" * 64,
        )
        assert await store.export_checkpoint(sink_id) == (
            float(last["created_at"]), str(last["request_id"]),
        )
        with pytest.raises(Exception, match="immutable"):
            await store.pool.execute(
                "UPDATE usage_events SET tokens=0 WHERE request_id=$1", request_ids[0],
            )
        with pytest.raises(Exception, match="immutable"):
            await store.pool.execute(
                "DELETE FROM usage_events WHERE request_id=$1", request_ids[0],
            )
        remaining = await store.usage_export(
            float(last["created_at"]), str(last["request_id"]), limit=10_000,
        )
        assert not [row for row in remaining if row["request_id"] in request_ids]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_jobs_recover_expired_leases_reject_stale_workers_and_cancel():
    store = PostgresJobStore(postgres_dsn())
    await store.initialize()
    suffix = uuid.uuid4().hex
    tenant_id = f"job-tenant-{suffix}"
    foreign_tenant_id = f"other-job-tenant-{suffix}"
    try:
        foreign = await store.create(foreign_tenant_id, "other-kind", {})
        await store.pool.execute(
            "UPDATE async_jobs SET created_at=$1 WHERE id=$2", -time.time() - 1, foreign["id"],
        )
        created = await store.create(tenant_id, "integration", {"value": 1})
        await store.pool.execute(
            "UPDATE async_jobs SET created_at=$1 WHERE id=$2", -time.time(), created["id"],
        )
        first = await store.claim(f"worker-a-{suffix}", 60, ("integration",))
        assert first and first["id"] == created["id"]
        assert first["payload"] == {"value": 1}

        await store.pool.execute(
            "UPDATE async_jobs SET lease_until=$1 WHERE id=$2", time.time() - 1, created["id"],
        )
        recovered = await store.claim(f"worker-b-{suffix}", 60, ("integration",))
        assert recovered and recovered["id"] == created["id"]
        assert recovered["attempts"] == 2
        assert not await store.complete(created["id"], f"worker-a-{suffix}", {"stale": True})
        event = {
            "id": created["id"], "tenant_id": tenant_id,
            "kind": "integration", "state": "succeeded",
        }
        assert await store.complete(
            created["id"], f"worker-b-{suffix}", {"ok": True}, notification=event,
        )
        completed = await store.get(tenant_id, created["id"])
        assert completed and completed["state"] == "succeeded"
        assert completed["result"] == {"ok": True}
        delivery = await store.claim_notification(f"notifier-a-{suffix}", 60)
        assert delivery and delivery["event"] == event
        await store.pool.execute(
            "UPDATE job_webhook_deliveries SET lease_until=$1 WHERE job_id=$2",
            time.time() - 1, created["id"],
        )
        recovered_delivery = await store.claim_notification(f"notifier-b-{suffix}", 60)
        assert recovered_delivery and recovered_delivery["attempts"] == 2
        assert not await store.finish_notification(
            created["id"], f"notifier-a-{suffix}", True,
        )
        assert await store.finish_notification(
            created["id"], f"notifier-b-{suffix}", True,
        )

        cancellable = await store.create(tenant_id, "integration", {"value": 2})
        cancelled = await store.cancel(tenant_id, cancellable["id"])
        assert cancelled and cancelled["state"] == "cancelled"
        assert await store.get(f"other-{suffix}", cancellable["id"]) is None
    finally:
        if store.pool:
            await store.pool.execute(
                """DELETE FROM job_webhook_deliveries WHERE job_id IN
                (SELECT id FROM async_jobs WHERE tenant_id=$1)""", tenant_id,
            )
            await store.pool.execute(
                "DELETE FROM async_jobs WHERE tenant_id=ANY($1::text[])",
                [tenant_id, foreign_tenant_id],
            )
        await store.close()


@pytest.mark.asyncio
async def test_postgres_coordinator_shares_circuits_and_concurrency_across_instances():
    first = PostgresCoordinator(postgres_dsn(), lease_seconds=0.1)
    second = PostgresCoordinator(postgres_dsn(), lease_seconds=0.1)
    await first.initialize()
    await second.initialize()
    suffix = uuid.uuid4().hex
    circuit_provider = f"circuit-{suffix}"
    slot_provider = f"slot-{suffix}"
    try:
        await first.failed(circuit_provider, threshold=1, cooldown_seconds=60)
        await second.refresh()
        assert not second.available(circuit_provider)
        await first.succeeded(circuit_provider)
        await second.refresh()
        assert second.available(circuit_provider)

        acquired = await asyncio.gather(
            first.acquire(slot_provider, 1), second.acquire(slot_provider, 1),
        )
        tokens = [token for token in acquired if token]
        assert len(tokens) == 1
        assert await first.acquire(slot_provider, 1) is None

        await asyncio.sleep(0.15)
        recovered = await second.acquire(slot_provider, 1)
        assert recovered is not None
        await second.release(recovered)

        async with first.slot(slot_provider, 1):
            await asyncio.sleep(0.15)
            assert await second.acquire(slot_provider, 1) is None
    finally:
        if first.pool:
            await first.pool.execute(
                "DELETE FROM provider_circuits WHERE provider=$1", circuit_provider,
            )
            await first.pool.execute(
                "DELETE FROM provider_concurrency_leases WHERE provider=$1", slot_provider,
            )
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_postgres_job_is_recovered_after_worker_process_termination():
    store = PostgresJobStore(postgres_dsn())
    await store.initialize()
    suffix = uuid.uuid4().hex
    tenant_id = f"process-tenant-{suffix}"
    kind = f"process-kind-{suffix}"
    job = await store.create(tenant_id, kind, {})
    script = (
        "import asyncio,os\nfrom llm_gateway.jobs import PostgresJobStore\n"
        f"kind={kind!r}\njob_id={job['id']!r}\n"
        "async def run():\n"
        " s=PostgresJobStore(os.environ['LLM_GATEWAY_TEST_POSTGRES_DSN']); await s.initialize(); "
        "j=await s.claim('terminated-worker',1,(kind,)); assert j and j['id']==job_id; await s.close()\n"
        "asyncio.run(run())"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=False,
        )
        assert result.returncode == 0, result.stderr
        await asyncio.sleep(1.1)
        recovered = await store.claim("recovery-worker", 60, (kind,))
        assert recovered and recovered["id"] == job["id"]
        assert recovered["attempts"] == 2
        assert await store.complete(job["id"], "recovery-worker", {"recovered": True})
    finally:
        if store.pool:
            await store.pool.execute("DELETE FROM async_jobs WHERE tenant_id=$1", tenant_id)
        await store.close()

import pytest

from llm_gateway.config import ProviderConfig
from llm_gateway.quota import QuotaStore


@pytest.mark.asyncio
async def test_quota_reservation_and_failed_request_release(tmp_path):
    store = QuotaStore(str(tmp_path / "quota.db"))
    await store.initialize()
    provider = ProviderConfig.model_validate(
        {
            "name": "free",
            "base_url": "https://example.test/v1",
            "api_key_env": "KEY",
            "limits": [{"requests": 1, "tokens": 10, "window_seconds": 60}],
            "models": [{"name": "chat", "upstream_model": "model"}],
        }
    )
    first = await store.reserve(provider, "one", 8)
    assert first is not None
    assert await store.reserve(provider, "two", 1) is None
    await store.reconcile(first, None, False)
    second = await store.reserve(provider, "two", 10)
    assert second is not None


@pytest.mark.asyncio
async def test_completed_usage_export_is_stable_bounded_and_cursor_paginated(tmp_path):
    store = QuotaStore(str(tmp_path / "export.db"))
    await store.initialize()
    provider = ProviderConfig.model_validate({
        "name": "paid", "base_url": "https://example.test/v1", "api_key_env": "KEY",
        "models": [{"name": "chat", "upstream_model": "model"}],
    })
    for request_id in ("one", "two"):
        reservation = await store.reserve(
            provider, request_id, 5, model="model", consumer_id="tenant",
        )
        assert reservation is not None
        await store.reconcile(reservation, 5, True, 3, 2)

    first = await store.usage_export(limit=1)
    second = await store.usage_export(first[0]["created_at"], first[0]["request_id"], 1)

    assert [first[0]["request_id"], second[0]["request_id"]] == ["one", "two"]
    assert first[0]["tenant_id"] == "tenant"
    assert first[0]["status"] == "completed"
    assert await store.export_checkpoint("sink") == (0, "")
    await store.save_export_checkpoint(
        "sink", float(first[0]["created_at"]), str(first[0]["request_id"]), "abc",
    )
    assert await store.export_checkpoint("sink") == (
        float(first[0]["created_at"]), str(first[0]["request_id"]),
    )


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


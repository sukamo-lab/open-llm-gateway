import pytest

from llm_gateway.identity import IdentityStore


@pytest.mark.asyncio
async def test_key_lifecycle_and_bounded_cache(tmp_path):
    store = IdentityStore(str(tmp_path / "identity.db"), cache_seconds=60, cache_max_entries=2)
    await store.initialize()
    tenant = await store.create_tenant("tenant", requests_per_minute=5)
    issued = await store.issue_key(tenant["id"], "service", expires_in_days=1)
    context = await store.authenticate(issued["api_key"])
    assert context and context.tenant_id == tenant["id"]
    await store.authenticate("invalid-one")
    await store.authenticate("invalid-two")
    assert len(store._cache) <= 2
    assert await store.revoke_key(issued["id"])
    assert await store.authenticate(issued["api_key"]) is None


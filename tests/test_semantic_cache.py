import pytest

from llm_gateway.config import SemanticCacheConfig
from llm_gateway.semantic_cache import MemorySemanticCache


@pytest.mark.asyncio
async def test_semantic_cache_is_tenant_version_and_request_scope_isolated():
    config = SemanticCacheConfig(similarity_threshold=0.5, dimensions=64)
    cache = MemorySemanticCache(config)
    request = {"model": "fast", "messages": [{"role": "user", "content": "reset my password"}]}
    response = {"id": "one", "choices": []}
    await cache.put("tenant-a", request, response)

    similar = {"model": "fast", "messages": [{"role": "user", "content": "password reset"}]}
    assert (await cache.get("tenant-a", similar))["id"] == "one"
    assert await cache.get("tenant-b", similar) is None
    assert await cache.get("tenant-a", {**similar, "model": "reasoning"}) is None

    cache.config = SemanticCacheConfig(
        embedding_version="local-hash-v2", similarity_threshold=0.5, dimensions=64,
    )
    assert await cache.get("tenant-a", similar) is None

import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.cache import MemoryCacheStore, SQLiteCacheStore, exact_cache_key
from llm_gateway.router import GatewayRouter


@pytest.mark.asyncio
async def test_memory_cache_is_tenant_scoped_and_bounded():
    store = MemoryCacheStore(ttl_seconds=60, max_entries=1)
    await store.initialize()
    await store.put("tenant-a", "one", '{"id":"a"}')
    assert (await store.get("tenant-a", "one")).response == {"id": "a"}
    assert await store.get("tenant-b", "one") is None
    await store.put("tenant-a", "two", '{"id":"b"}')
    assert await store.get("tenant-a", "one") is None
    assert (await store.get("tenant-a", "two")).response == {"id": "b"}


@pytest.mark.asyncio
async def test_sqlite_cache_persists_hashed_key_and_response(tmp_path):
    store = SQLiteCacheStore(str(tmp_path / "cache.db"), ttl_seconds=60, max_entries=10)
    await store.initialize()
    key = exact_cache_key({"messages": [{"content": "private prompt"}]}, "v1", "routing")
    assert "private" not in key
    await store.put("tenant", key, '{"id":"cached"}')
    entry = await store.get("tenant", key)
    assert entry and entry.response == {"id": "cached"}


def test_chat_exact_cache_hit_and_client_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "secret")
    config = tmp_path / "config.yaml"
    config.write_text(
        "features:\n  exact_cache: true\n"
        "cache:\n  backend: memory\n  ttl_seconds: 60\n"
        "providers:\n  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_env: PROVIDER_KEY\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )
    calls = 0

    async def complete(
        _router, _payload, _consumer_id=None, _consumer_limits=(),
        _require_injection_safe=False,
    ):
        nonlocal calls
        calls += 1
        return {
            "id": f"response-{calls}", "choices": [],
            "_gateway_meta": {"rate_limits": {"x-ratelimit-remaining": "1"}},
        }, "p", "model"

    monkeypatch.setattr(GatewayRouter, "complete_for", complete)
    body = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}
    with TestClient(create_app(str(config))) as client:
        first = client.post("/v1/chat/completions", json=body)
        second = client.post("/v1/chat/completions", json=body)
        bypass = client.post(
            "/v1/chat/completions", json=body, headers={"Cache-Control": "no-store"}
        )

    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["gateway"]["rate_limits"] == {}
    assert bypass.headers["X-Cache"] == "BYPASS"
    assert calls == 2

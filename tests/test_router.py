import httpx
import pytest

from llm_gateway.config import GatewayConfig
from llm_gateway.quota import QuotaStore
from llm_gateway.router import GatewayRouter


def make_config(tmp_path, providers):
    return GatewayConfig.model_validate(
        {"database": str(tmp_path / "usage.db"), "providers": providers}
    )


def provider(name, tier, key_env, models, priority_limit=None):
    result = {
        "name": name,
        "base_url": f"https://{name}.example/v1",
        "api_key_env": key_env,
        "tier": tier,
        "models": models,
    }
    if priority_limit:
        result["limits"] = priority_limit
    return result


def test_free_models_precede_paid_and_reasoning_is_required(tmp_path, monkeypatch):
    for key in ("FREE_KEY", "PAID_KEY"):
        monkeypatch.setenv(key, "secret")
    config = make_config(
        tmp_path,
        [
            provider(
                "paid",
                "paid",
                "PAID_KEY",
                [
                    {"name": "chat", "upstream_model": "paid-chat", "priority": 1},
                    {"name": "reasoning", "upstream_model": "paid-reason", "reasoning": True},
                ],
            ),
            provider(
                "free",
                "free",
                "FREE_KEY",
                [{"name": "chat", "upstream_model": "free-chat", "priority": 999}],
            ),
        ],
    )
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    assert [item.provider.name for item in router.candidates("chat", False)] == ["free", "paid"]
    assert [item.provider.name for item in router.candidates("chat", False, "paid")] == ["paid"]
    assert [item.model.upstream_model for item in router.candidates("auto", True)] == ["paid-reason"]


@pytest.mark.asyncio
async def test_retryable_response_fails_over(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    monkeypatch.setenv("PAID_KEY", "secret")
    config = make_config(
        tmp_path,
        [
            provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "free"}]),
            provider("paid", "paid", "PAID_KEY", [{"name": "chat", "upstream_model": "paid"}]),
        ],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "free.example":
            return httpx.Response(429, json={"error": "limited"})
        return httpx.Response(
            200,
            json={
                "id": "ok",
                "model": "paid",
                "choices": [],
                "usage": {"total_tokens": 7},
            },
        )

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = GatewayRouter(config, quota, client)
        body, selected, _ = await router.complete({"model": "chat", "messages": []})
    assert body["id"] == "ok"
    assert selected == "paid"
    snapshot = await quota.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["provider"] == "paid"
    assert snapshot[0]["requests"] == 1
    assert snapshot[0]["tokens"] == 7


@pytest.mark.asyncio
async def test_non_retryable_provider_error_also_fails_over(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    monkeypatch.setenv("PAID_KEY", "secret")
    config = make_config(
        tmp_path,
        [
            provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "removed"}]),
            provider("paid", "paid", "PAID_KEY", [{"name": "chat", "upstream_model": "working"}]),
        ],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "free.example":
            return httpx.Response(400, json={"error": {"message": "model was removed"}})
        return httpx.Response(200, json={"id": "ok", "choices": [], "usage": {"total_tokens": 1}})

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, selected, _ = await GatewayRouter(config, quota, client).complete(
            {"model": "chat", "messages": []}
        )
    assert body["id"] == "ok"
    assert selected == "paid"


@pytest.mark.asyncio
async def test_empty_success_response_fails_over(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    monkeypatch.setenv("PAID_KEY", "secret")
    config = make_config(
        tmp_path,
        [
            provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "broken"}]),
            provider("paid", "paid", "PAID_KEY", [{"name": "chat", "upstream_model": "working"}]),
        ],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "free.example":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"id": "ok", "choices": [], "usage": {"total_tokens": 1}})

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, selected, _ = await GatewayRouter(config, quota, client).complete(
            {"model": "chat", "messages": []}
        )
    assert body["id"] == "ok"
    assert selected == "paid"


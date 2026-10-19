import httpx
import pytest
from contextlib import asynccontextmanager

from llm_gateway.config import GatewayConfig, RoutingPolicyConfig
from llm_gateway.metrics import Metrics
from llm_gateway.quota import NullQuotaStore, QuotaStore
from llm_gateway.router import GatewayRouter, NoModelAvailable


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


class BrokenAfterFirstChunk(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: first\n\n"
        raise httpx.ReadError("stream interrupted")


class FakeCoordinator:
    def __init__(self):
        self.open = False
        self.events = []

    async def refresh(self):
        self.events.append("refresh")

    def available(self, _provider):
        return not self.open

    async def failed(self, provider_name, threshold, cooldown):
        self.events.append(("failed", provider_name, threshold, cooldown))
        self.open = True

    async def succeeded(self, provider_name):
        self.events.append(("succeeded", provider_name))
        self.open = False

    @asynccontextmanager
    async def slot(self, provider_name, limit):
        self.events.append(("slot", provider_name, limit))
        yield


@pytest.mark.asyncio
async def test_router_uses_distributed_circuit_and_concurrency_coordinator(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY", "test-key")
    config = make_config(tmp_path, [provider(
        "shared", "free", "KEY", [{"name": "chat", "upstream_model": "model"}],
    )])
    coordinator = FakeCoordinator()
    async with httpx.AsyncClient() as client:
        router = GatewayRouter(config, NullQuotaStore(), client, coordinator=coordinator)
        candidate = router.candidates("chat", False, None, {"chat"})[0]
        await router._failed(candidate)
        assert not router._available(candidate.provider)
        await router._succeeded(candidate)
        assert router._available(candidate.provider)
        async with router._slot(candidate.provider):
            pass
    assert ("slot", "shared", 10) in coordinator.events


@pytest.mark.asyncio
async def test_overlapping_provider_credentials_fall_back_without_exposure(tmp_path, monkeypatch):
    monkeypatch.setenv("NEW_PROVIDER_KEY", "new-secret")
    monkeypatch.setenv("OLD_PROVIDER_KEY", "old-secret")
    config = make_config(tmp_path, [{
        "name": "rotating", "base_url": "https://rotating.example/v1", "tier": "free",
        "credentials": [
            {"id": "new", "api_key_env": "NEW_PROVIDER_KEY"},
            {"id": "old", "api_key_env": "OLD_PROVIDER_KEY"},
        ],
        "models": [{"name": "chat", "upstream_model": "model"}],
    }])
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer new-secret":
            return httpx.Response(401, json={"error": "invalid"})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"total_tokens": 1},
        })

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, selected, _ = await GatewayRouter(config, quota, client).complete_for({
            "model": "chat", "messages": [{"role": "user", "content": "hello"}],
        })
    assert body["choices"][0]["message"]["content"] == "ok"
    assert selected == "rotating"
    assert seen == ["Bearer new-secret", "Bearer old-secret"]
    dumped = str(config.model_dump(mode="json"))
    assert "new-secret" not in dumped
    assert "old-secret" not in dumped


@pytest.mark.asyncio
async def test_stream_uses_next_overlapping_credential_before_emitting_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("NEW_PROVIDER_KEY", "new-secret")
    monkeypatch.setenv("OLD_PROVIDER_KEY", "old-secret")
    config = make_config(tmp_path, [{
        "name": "rotating", "base_url": "https://rotating.example/v1", "tier": "free",
        "credentials": [
            {"id": "new", "api_key_env": "NEW_PROVIDER_KEY"},
            {"id": "old", "api_key_env": "OLD_PROVIDER_KEY"},
        ],
        "models": [{"name": "chat", "upstream_model": "model"}],
    }])
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer new-secret":
            return httpx.Response(403, content=b"rejected")
        return httpx.Response(200, content=b"data: accepted\n\n")

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = [chunk async for chunk in GatewayRouter(config, quota, client).stream({
            "model": "chat", "messages": [{"role": "user", "content": "hello"}],
        })]

    assert b"".join(chunks) == b"data: accepted\n\n"
    assert seen == ["Bearer new-secret", "Bearer old-secret"]


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


def test_cost_routing_orders_within_tier_and_preserves_free_first(tmp_path, monkeypatch):
    for key in ("FREE_A", "FREE_B", "PAID_KEY"):
        monkeypatch.setenv(key, "secret")
    config = make_config(tmp_path, [
        provider("free-expensive", "free", "FREE_A", [{
            "name": "chat", "upstream_model": "free-expensive", "priority": 1,
            "input_cost_per_million": 2, "output_cost_per_million": 8,
            "pricing_version": "2026-10",
        }]),
        provider("free-cheap", "free", "FREE_B", [{
            "name": "chat", "upstream_model": "free-cheap", "priority": 100,
            "input_cost_per_million": 1, "output_cost_per_million": 2,
            "pricing_version": "2026-10",
        }]),
        provider("paid", "paid", "PAID_KEY", [{
            "name": "chat", "upstream_model": "paid-cheapest", "priority": 1,
            "input_cost_per_million": 0.01, "output_cost_per_million": 0.01,
            "pricing_version": "2026-10",
        }]),
    ])
    config.features.cost_routing = True
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    ranked = router.rank_by_cost(router.candidates("chat", False), 100, 100)
    assert [item.model.upstream_model for item in ranked] == [
        "free-cheap", "free-expensive", "paid-cheapest",
    ]


def test_cost_routing_unknown_price_policy_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    definitions = provider("free", "free", "FREE_KEY", [
        {"name": "unknown", "upstream_model": "unknown", "priority": 1},
        {
            "name": "known", "upstream_model": "known-free", "priority": 2,
            "pricing_version": "2026-10",
        },
    ])
    config = make_config(tmp_path, [definitions])
    config.features.cost_routing = True
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidates = router.candidates("auto", False)
    assert [item.model.upstream_model for item in router.rank_by_cost(candidates, 10, 10)] == [
        "known-free", "unknown",
    ]
    config.routing.unknown_price_policy = "exclude"
    assert [item.model.upstream_model for item in router.rank_by_cost(candidates, 10, 10)] == [
        "known-free",
    ]


@pytest.mark.asyncio
async def test_cost_routing_exposes_selected_price_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(tmp_path, [provider("free", "free", "FREE_KEY", [{
        "name": "chat", "upstream_model": "vendor-chat",
        "input_cost_per_million": 1, "output_cost_per_million": 2,
        "pricing_version": "2026-10",
    }])])
    config.features.cost_routing = True

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        })

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, _provider, _model = await GatewayRouter(config, quota, client).complete_for({
            "model": "chat", "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
        })
    assert body["_gateway_meta"]["routing"]["strategy"] == "least_cost"
    assert body["_gateway_meta"]["routing"]["pricing_version"] == "2026-10"
    assert body["_gateway_meta"]["routing"]["estimated_cost_usd"] > 0


def test_latency_routing_uses_ewma_and_preserves_free_first(tmp_path, monkeypatch):
    for key in ("FREE_A", "FREE_B", "PAID_KEY"):
        monkeypatch.setenv(key, "secret")
    config = make_config(tmp_path, [
        provider("slow", "free", "FREE_A", [{"name": "slow", "upstream_model": "slow"}]),
        provider("fast", "free", "FREE_B", [{"name": "fast", "upstream_model": "fast"}]),
        provider("paid", "paid", "PAID_KEY", [{"name": "paid", "upstream_model": "paid"}]),
    ])
    config.features.latency_routing = True
    config.routing.latency_ewma_alpha = 0.5
    config.routing.latency_min_samples = 2
    config.routing.latency_exploration_interval = 0
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidates = router.candidates("auto", False)
    by_name = {candidate.model.name: candidate for candidate in candidates}
    for value in (1000, 800):
        router._observe_latency(by_name["slow"], "chat", value)
    for value in (100, 80):
        router._observe_latency(by_name["fast"], "chat", value)
    for value in (10, 8):
        router._observe_latency(by_name["paid"], "chat", value)
    ranked = router.rank_by_latency(candidates, "chat")
    assert [candidate.model.name for candidate in ranked] == ["fast", "slow", "paid"]
    assert router.latency_state(by_name["slow"], "chat")["ewma_ms"] == 900


def test_residency_enforcement_cannot_fallback_or_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("EU_KEY", "secret")
    monkeypatch.setenv("US_KEY", "secret")
    config = GatewayConfig.model_validate({
        "database": str(tmp_path / "usage.db"),
        "features": {"routing_policy": True, "residency_enforcement": True},
        "residency": {"policy": "eu-only"},
        "routing": {"allow_policy_override": True, "policies": [{
            "name": "eu-only", "residencies": ["eu"], "regions": ["eu-west"], "fallback": "deny",
        }, {"name": "unrestricted", "fallback": "default"}]},
        "providers": [
            {**provider("eu", "free", "EU_KEY", [{"name": "chat", "upstream_model": "eu"}]),
             "region": "eu-west", "residencies": ["eu"]},
            {**provider("us", "free", "US_KEY", [{"name": "chat", "upstream_model": "us"}]),
             "region": "us-east", "residencies": ["us"]},
        ],
    })
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidates, selected = router.apply_routing_policy(router.candidates("chat", False), "chat", "chat")
    assert selected and selected.name == "eu-only"
    assert [item.provider.name for item in candidates] == ["eu"]
    with pytest.raises(NoModelAvailable, match="cannot bypass"):
        router.apply_routing_policy(
            router.candidates("chat", False), "chat", "chat", "unrestricted"
        )


def test_latency_routing_explores_least_sampled_deterministically(tmp_path, monkeypatch):
    for key in ("A", "B"):
        monkeypatch.setenv(key, "secret")
    config = make_config(tmp_path, [
        provider("a", "free", "A", [{"name": "a", "upstream_model": "a", "priority": 1}]),
        provider("b", "free", "B", [{"name": "b", "upstream_model": "b", "priority": 2}]),
    ])
    config.features.latency_routing = True
    config.routing.latency_min_samples = 1
    config.routing.latency_exploration_interval = 2
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidates = router.candidates("auto", False)
    router._observe_latency(candidates[0], "chat", 50)
    router._observe_latency(candidates[0], "chat", 50)
    router._observe_latency(candidates[1], "chat", 500)
    assert router.rank_by_latency(candidates, "chat")[0].model.name == "a"
    assert router.rank_by_latency(candidates, "chat")[0].model.name == "b"


def test_latency_exploration_intervals_are_isolated_by_operation(tmp_path, monkeypatch):
    for key in ("A", "B"):
        monkeypatch.setenv(key, "secret")
    config = make_config(tmp_path, [
        provider("a", "free", "A", [{"name": "a", "upstream_model": "a", "priority": 1}]),
        provider("b", "free", "B", [{"name": "b", "upstream_model": "b", "priority": 2}]),
    ])
    config.features.latency_routing = True
    config.routing.latency_min_samples = 1
    config.routing.latency_exploration_interval = 2
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidates = router.candidates("auto", False)
    router._observe_latency(candidates[0], "chat", 50)
    router._observe_latency(candidates[0], "chat", 50)
    router._observe_latency(candidates[1], "chat", 500)

    assert router.rank_by_latency(candidates, "chat")[0].model.name == "a"
    assert router.rank_by_latency(candidates, "embeddings")[0].model.name == "a"
    assert router.rank_by_latency(candidates, "chat")[0].model.name == "b"


def test_routing_policy_enforces_region_residency_quality_and_fallback(tmp_path, monkeypatch):
    for key in ("EU", "US"):
        monkeypatch.setenv(key, "secret")
    eu = provider("eu", "free", "EU", [{
        "name": "chat", "upstream_model": "eu-chat", "quality": 90,
    }])
    eu.update({"region": "eu-west", "residencies": ["eu"]})
    us = provider("us", "free", "US", [{
        "name": "chat", "upstream_model": "us-chat", "quality": 95,
    }])
    us.update({"region": "us-east", "residencies": ["us"]})
    config = make_config(tmp_path, [us, eu])
    config.features.routing_policy = True
    config.routing.policies = [RoutingPolicyConfig.model_validate({
        "name": "eu-only", "operations": ["chat"], "regions": ["eu-west"],
        "residencies": ["eu"], "min_quality": 80,
    })]
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidates, policy = router.apply_routing_policy(
        router.candidates("chat", False), "chat", "chat"
    )
    assert policy is not None and policy.name == "eu-only"
    assert [candidate.provider.name for candidate in candidates] == ["eu"]

    config.routing.policies[0] = config.routing.policies[0].model_copy(update={
        "regions": {"missing"}, "fallback": "default",
    })
    candidates, _policy = router.apply_routing_policy(
        router.candidates("chat", False), "chat", "chat"
    )
    assert [candidate.provider.name for candidate in candidates] == ["eu", "us"]


def test_named_routing_policy_override_is_guarded(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY", "secret")
    config = make_config(tmp_path, [
        provider("p", "free", "KEY", [{"name": "chat", "upstream_model": "chat"}]),
    ])
    config.features.routing_policy = True
    config.routing.policies = [RoutingPolicyConfig(name="restricted")]
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    with pytest.raises(NoModelAvailable, match="override is disabled"):
        router.apply_routing_policy(router.candidates("chat", False), "chat", "chat", "restricted")
    config.routing.allow_policy_override = True
    _candidates, policy = router.apply_routing_policy(
        router.candidates("chat", False), "chat", "chat", "restricted"
    )
    assert policy is not None and policy.name == "restricted"
    upstream = router._upstream_payload(
        router.candidates("chat", False)[0],
        {"model": "chat", "messages": [], "routing_policy": "restricted"},
        False,
    )
    assert "routing_policy" not in upstream


@pytest.mark.asyncio
async def test_successful_request_records_explainable_latency_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(tmp_path, [provider(
        "free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "vendor-chat"}],
    )])
    config.features.latency_routing = True

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = GatewayRouter(config, quota, client)
        body, _provider, _model = await router.complete_for({
            "model": "chat", "messages": [{"role": "user", "content": "hello"}],
        })
    routing = body["_gateway_meta"]["routing"]
    assert routing["strategy"] == "latency"
    assert routing["latency"]["samples"] == 1
    assert routing["latency"]["ewma_ms"] is not None


def test_prompt_injection_route_only_selects_marked_safe_models(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    monkeypatch.setenv("PAID_KEY", "secret")
    config = make_config(tmp_path, [
        provider("free", "free", "FREE_KEY", [
            {"name": "chat", "upstream_model": "free-chat", "priority": 1},
        ]),
        provider("paid", "paid", "PAID_KEY", [
            {
                "name": "safe", "upstream_model": "safe-chat", "priority": 1,
                "prompt_injection_safe": True,
            },
        ]),
    ])
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    assert [item.model.name for item in router.candidates("auto", False)] == ["chat", "safe"]
    assert [
        item.model.name for item in router.candidates(
            "auto", False, require_injection_safe=True
        )
    ] == ["safe"]


def test_gateway_request_context_is_never_forwarded_upstream(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(tmp_path, [
        provider("free", "free", "FREE_KEY", [
            {"name": "chat", "upstream_model": "free-chat"},
        ]),
    ])
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidate = router.candidates("chat", False)[0]
    upstream = router._upstream_payload(candidate, {
        "model": "chat", "messages": [],
        "_gateway_request_context": {"api": "responses", "metadata": {"key": "value"}},
    }, False)
    assert "_gateway_request_context" not in upstream


def test_chat_and_embedding_provider_defaults_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    definition = provider("free", "free", "FREE_KEY", [{
        "name": "both", "upstream_model": "model", "capabilities": ["chat", "embeddings"],
    }])
    definition["request_defaults"] = {"temperature": 0.2}
    definition["embeddings_request_defaults"] = {"encoding_format": "base64"}
    config = make_config(tmp_path, [definition])
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    candidate = router.candidates("both", False)[0]
    chat_payload = router._upstream_payload(candidate, {"messages": []}, False)
    embedding_payload = router._embedding_payload(candidate, {"input": "hello"})
    assert chat_payload["temperature"] == 0.2
    assert "encoding_format" not in chat_payload
    assert embedding_payload["encoding_format"] == "base64"
    assert "temperature" not in embedding_payload


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


@pytest.mark.asyncio
async def test_transient_response_retries_same_provider_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(
        tmp_path,
        [provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "free"}])],
    )
    config.routing.retry_attempts = 2
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "limited"})
        return httpx.Response(200, json={"id": "ok", "choices": [], "usage": {"total_tokens": 1}})

    quota = QuotaStore(config.database)
    await quota.initialize()
    metrics = Metrics()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, selected, _ = await GatewayRouter(config, quota, client, metrics).complete(
            {"model": "chat", "messages": []}
        )
    assert selected == "free"
    assert calls == 2
    assert body["_gateway_meta"]["upstream_attempts"] == 2
    assert metrics.provider_requests[("free", "chat", "free", "error", "429")] == 1
    assert metrics.provider_requests[("free", "chat", "free", "success", "200")] == 1
    assert metrics.retries[("free", "chat", "http_429")] == 1
    assert metrics.provider_results[("free", "chat", "free", "success")] == 1


@pytest.mark.asyncio
async def test_non_transient_error_does_not_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(
        tmp_path,
        [provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "free"}])],
    )
    config.routing.retry_attempts = 3
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "invalid"})

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception):
            await GatewayRouter(config, quota, client).complete({"model": "chat", "messages": []})
    assert calls == 1


@pytest.mark.asyncio
async def test_stream_retries_retryable_status_before_emitting_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(
        tmp_path,
        [provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "free"}])],
    )
    config.routing.retry_attempts = 2
    config.routing.retry_initial_backoff_seconds = 0
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, content=b"limited")
        return httpx.Response(200, content=b"data: success\n\n")

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = [chunk async for chunk in GatewayRouter(config, quota, client).stream({
            "model": "chat", "messages": [{"role": "user", "content": "hello"}],
        })]

    assert b"".join(chunks) == b"data: success\n\n"
    assert calls == 2


@pytest.mark.asyncio
async def test_stream_retries_transport_failure_before_emitting_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(
        tmp_path,
        [provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "free"}])],
    )
    config.routing.retry_attempts = 2
    config.routing.retry_initial_backoff_seconds = 0
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, content=b"data: recovered\n\n")

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = [chunk async for chunk in GatewayRouter(config, quota, client).stream({
            "model": "chat", "messages": [],
        })]

    assert b"".join(chunks) == b"data: recovered\n\n"
    assert calls == 2


@pytest.mark.asyncio
async def test_stream_never_retries_or_fails_over_after_emitting_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    monkeypatch.setenv("PAID_KEY", "secret")
    config = make_config(tmp_path, [
        provider("free", "free", "FREE_KEY", [{"name": "chat", "upstream_model": "free"}]),
        provider("paid", "paid", "PAID_KEY", [{"name": "chat", "upstream_model": "paid"}]),
    ])
    config.routing.retry_attempts = 2
    config.routing.retry_initial_backoff_seconds = 0
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "free.example":
            return httpx.Response(200, stream=BrokenAfterFirstChunk())
        return httpx.Response(200, content=b"data: paid\n\n")

    quota = QuotaStore(config.database)
    await quota.initialize()
    received = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadError):
            async for chunk in GatewayRouter(config, quota, client).stream({
                "model": "chat", "messages": [],
            }):
                received.append(chunk)

    assert received == [b"data: first\n\n"]
    assert calls == ["free.example"]


@pytest.mark.asyncio
async def test_embeddings_use_capability_path_alias_and_usage_accounting(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(tmp_path, [provider(
        "free", "free", "FREE_KEY",
        [{
            "name": "embed", "upstream_model": "vendor-embed",
            "capabilities": ["embeddings"], "input_cost_per_million": 1,
        }],
    )])
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "model": "vendor-embed",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        })

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, selected, upstream = await GatewayRouter(config, quota, client).embed_for({
            "model": "embed", "input": "hello", "encoding_format": "float",
        })
    assert seen["path"].endswith("/v1/embeddings")
    assert b'"model":"vendor-embed"' in seen["body"]
    assert selected == "free"
    assert upstream == "vendor-embed"
    assert body["model"] == "embed"
    assert body["gateway"]["provider"] == "free"
    snapshot = await quota.snapshot()
    assert snapshot[0]["tokens"] == 2


def test_embedding_candidates_exclude_chat_only_models(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(tmp_path, [provider(
        "free", "free", "FREE_KEY",
        [
            {"name": "chat", "upstream_model": "chat-model"},
            {"name": "embed", "upstream_model": "embed-model", "capabilities": ["embeddings"]},
        ],
    )])
    router = GatewayRouter(config, QuotaStore(config.database), httpx.AsyncClient())
    assert [candidate.model.name for candidate in router.candidates(
        "auto", False, required_capabilities={"embeddings"}
    )] == ["embed"]


@pytest.mark.asyncio
async def test_speech_uses_capability_path_alias_and_binary_response(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(tmp_path, [provider(
        "free", "free", "FREE_KEY",
        [{"name": "speak", "upstream_model": "vendor-tts", "capabilities": ["audio_speech"]}],
    )])
    config.features.audio_api = True
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, content=b"audio-bytes", headers={"content-type": "audio/mpeg"})

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        content, selected, upstream, _headers = await GatewayRouter(config, quota, client).speech_for({
            "model": "speak", "input": "hello", "voice": "alloy", "response_format": "mp3",
        })
    assert seen["path"].endswith("/v1/audio/speech")
    assert b'"model":"vendor-tts"' in seen["body"]
    assert content == b"audio-bytes"
    assert (selected, upstream) == ("free", "vendor-tts")


@pytest.mark.asyncio
async def test_images_use_capability_path_alias_and_validate_response(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_KEY", "secret")
    config = make_config(tmp_path, [provider(
        "free", "free", "FREE_KEY",
        [{"name": "image", "upstream_model": "vendor-image", "capabilities": ["image_generation"]}],
    )])
    config.features.images_api = True
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={
            "created": 100, "data": [{"url": "https://example.test/image.png"}],
        })

    quota = QuotaStore(config.database)
    await quota.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, selected, upstream = await GatewayRouter(config, quota, client).generate_images_for({
            "model": "image", "prompt": "lighthouse", "n": 1, "response_format": "url",
        })
    assert seen["path"].endswith("/v1/images/generations")
    assert b'"model":"vendor-image"' in seen["body"]
    assert body["data"][0]["url"].endswith("image.png")
    assert body["gateway"]["provider"] == "free"
    assert (selected, upstream) == ("free", "vendor-image")


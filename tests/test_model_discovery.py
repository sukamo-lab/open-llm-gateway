import httpx
import pytest

from llm_gateway.config import GatewayConfig, ModelDiscoveryConfig
from llm_gateway.model_discovery import discover_models, eligible, parse_model_ids


def test_catalog_parsing_and_filters_are_bounded():
    assert parse_model_ids({"data": [{"id": "text-fast"}, {"id": "text-fast"}]}, 10) == ["text-fast"]
    config = ModelDiscoveryConfig(allow_patterns=["text-*"], deny_patterns=["*-preview"])
    assert eligible("text-fast", config)
    assert not eligible("text-preview", config)
    assert not eligible("image-fast", config)
    with pytest.raises(Exception, match="oversized"):
        parse_model_ids({"data": [{"id": "one"}, {"id": "two"}]}, 1)


@pytest.mark.asyncio
async def test_discovery_reports_unconfigured_and_missing_models(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "secret")
    config = GatewayConfig.model_validate({
        "features": {"admin_api": True, "model_discovery": True},
        "model_discovery": {"allow_patterns": ["model-*"]},
        "providers": [{
            "name": "p", "base_url": "https://example.test/v1", "api_key_env": "PROVIDER_KEY",
            "models": [{"name": "configured", "upstream_model": "model-old"}],
        }],
    })

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"data": [{"id": "model-new"}, {"id": "other"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reports = await discover_models(config, client)
    assert reports[0]["eligible"] == ["model-new"]
    assert reports[0]["unconfigured"] == ["model-new"]
    assert reports[0]["configured_missing"] == ["model-old"]

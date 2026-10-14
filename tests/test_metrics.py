import pytest

from llm_gateway.metrics import Metrics


@pytest.mark.asyncio
async def test_metrics_render_histograms_and_bounded_labels():
    metrics = Metrics(http_buckets=(0.1, 1.0), provider_buckets=(0.5, 2.0))
    endpoint = '/v1/"chat"\n'

    await metrics.request(endpoint, 200, 250)
    await metrics.provider("free-provider", "success", "chat", "free")
    await metrics.provider_request("free-provider", "chat", "free", "success", "200", 750)
    await metrics.retry("free-provider", "chat", "http_429")
    await metrics.cache("memory", "hit")
    await metrics.circuit("free-provider", True)
    await metrics.pii("input", "redact", "matched")
    await metrics.moderation("input", "block", "blocked")
    rendered = await metrics.render()

    escaped_endpoint = 'endpoint="/v1/\\"chat\\"\\n"'
    assert f"llm_gateway_http_requests_total{{{escaped_endpoint},status=\"200\"}} 1" in rendered
    assert 'llm_gateway_provider_attempts_total{provider="free-provider",outcome="success"} 1' in rendered
    assert 'llm_gateway_provider_results_total{provider="free-provider",model="chat",tier="free",outcome="success"} 1' in rendered
    assert 'llm_gateway_provider_requests_total{provider="free-provider",model="chat",tier="free",outcome="success",status="200"} 1' in rendered
    assert 'llm_gateway_provider_retries_total{provider="free-provider",model="chat",reason="http_429"} 1' in rendered
    assert 'llm_gateway_cache_requests_total{backend="memory",outcome="hit"} 1' in rendered
    assert 'llm_gateway_provider_circuit_open{provider="free-provider"} 1' in rendered
    assert 'llm_gateway_pii_events_total{stage="input",action="redact",outcome="matched"} 1' in rendered
    assert 'llm_gateway_moderation_events_total{stage="input",action="block",outcome="blocked"} 1' in rendered
    assert f"llm_gateway_http_request_duration_seconds_bucket{{{escaped_endpoint},le=\"0.1\"}} 0" in rendered
    assert f"llm_gateway_http_request_duration_seconds_bucket{{{escaped_endpoint},le=\"1\"}} 1" in rendered
    assert f"llm_gateway_http_request_duration_seconds_bucket{{{escaped_endpoint},le=\"+Inf\"}} 1" in rendered
    assert f"llm_gateway_http_request_duration_seconds_sum{{{escaped_endpoint}}} 0.250000000" in rendered
    assert f"llm_gateway_http_request_duration_seconds_count{{{escaped_endpoint}}} 1" in rendered
    assert 'llm_gateway_provider_request_duration_seconds_bucket{provider="free-provider",model="chat",tier="free",le="0.5"} 0' in rendered
    assert 'llm_gateway_provider_request_duration_seconds_bucket{provider="free-provider",model="chat",tier="free",le="2"} 1' in rendered
    assert "private-value" not in rendered

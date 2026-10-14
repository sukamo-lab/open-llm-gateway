import json
import logging

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from llm_gateway.config import TelemetryConfig
from llm_gateway.logging_config import JsonFormatter
from llm_gateway.telemetry import Telemetry


def test_w3c_parent_provider_span_and_log_correlation_are_content_safe():
    exporter = InMemorySpanExporter()
    config = TelemetryConfig(excluded_paths=set(), include_tenant_id=False)
    telemetry = Telemetry(config, exporter=exporter)
    trace_id = "0123456789abcdef0123456789abcdef"
    handle = telemetry.start_request(
        {
            "traceparent": f"00-{trace_id}-0123456789abcdef-01",
            "authorization": "Bearer must-not-appear",
            "x-prompt": "private prompt",
        },
        "POST", "/v1/chat/completions", "request-1",
    )
    telemetry.set_tenant("private-tenant")
    provider = telemetry.start_provider("provider", "fast", "upstream-model", 1)
    telemetry.finish_provider(provider, status_code=200)

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "complete", (), None)
    event = json.loads(JsonFormatter().format(record))
    telemetry.finish_request(handle, 200)
    telemetry.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    server = next(span for span in spans if span.name == "POST /v1/chat/completions")
    client = next(span for span in spans if span.name == "llm.provider.request")
    assert f"{server.context.trace_id:032x}" == trace_id
    assert client.parent.span_id == server.context.span_id
    assert "tenant.id" not in server.attributes
    serialized = json.dumps([dict(span.attributes) for span in spans])
    assert "must-not-appear" not in serialized
    assert "private prompt" not in serialized
    assert event["trace.id"] == trace_id


def test_excluded_health_path_creates_no_span():
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(TelemetryConfig(), exporter=exporter)
    assert telemetry.start_request({}, "GET", "/health", "request-2") is None
    telemetry.finish_request(None, 200)
    telemetry.shutdown()
    assert exporter.get_finished_spans() == ()

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from opentelemetry import context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import __version__


@dataclass(frozen=True)
class SpanHandle:
    span: Any
    token: object


class Telemetry:
    """Content-safe manual tracing with no prompt, response, key, or header capture."""

    def __init__(self, config, exporter=None):
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            options: dict[str, Any] = {"timeout": config.export_timeout_seconds}
            if config.endpoint():
                options["endpoint"] = config.endpoint()
            exporter = OTLPSpanExporter(**options)
        self.config = config
        self.provider = TracerProvider(
            sampler=ParentBased(TraceIdRatioBased(config.sample_ratio)),
            resource=Resource.create({
                "service.name": "llm-gateway",
                "service.version": __version__,
                "telemetry.sdk.language": "python",
            }),
        )
        self.provider.add_span_processor(BatchSpanProcessor(
            exporter,
            max_queue_size=config.max_queue_size,
            schedule_delay_millis=config.schedule_delay_millis,
            max_export_batch_size=config.max_export_batch_size,
            export_timeout_millis=int(config.export_timeout_seconds * 1000),
        ))
        self.tracer = self.provider.get_tracer("llm_gateway", __version__)

    def start_request(
        self, headers: Mapping[str, str], method: str, path: str, request_id: str
    ) -> SpanHandle | None:
        if path in self.config.excluded_paths:
            return None
        parent = propagate.extract(carrier=dict(headers))
        span = self.tracer.start_span(
            f"{method} {path}", context=parent, kind=SpanKind.SERVER,
            attributes={
                "http.request.method": method,
                "url.path": path,
                "http.route": path,
                "request.id": request_id,
            },
        )
        token = context.attach(trace.set_span_in_context(span, parent))
        return SpanHandle(span, token)

    def finish_request(
        self, handle: SpanHandle | None, status_code: int, error_type: str | None = None
    ) -> None:
        if handle is None:
            return
        handle.span.set_attribute("http.response.status_code", status_code)
        if error_type:
            handle.span.set_attribute("error.type", error_type)
        if status_code >= 500 or error_type:
            handle.span.set_status(Status(StatusCode.ERROR))
        context.detach(handle.token)
        handle.span.end()

    def set_tenant(self, tenant_id: str) -> None:
        if self.config.include_tenant_id:
            span = trace.get_current_span()
            if span.is_recording():
                span.set_attribute("tenant.id", tenant_id)

    def start_provider(
        self, provider: str, model: str, upstream_model: str, attempt: int,
        operation: str = "chat",
    ) -> SpanHandle:
        span = self.tracer.start_span(
            "llm.provider.request", kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": operation,
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": upstream_model,
                "llm.gateway.model.alias": model,
                "llm.gateway.attempt": attempt,
            },
        )
        token = context.attach(trace.set_span_in_context(span))
        return SpanHandle(span, token)

    @staticmethod
    def finish_provider(
        handle: SpanHandle, status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        if status_code is not None:
            handle.span.set_attribute("http.response.status_code", status_code)
        if error_type:
            handle.span.set_attribute("error.type", error_type)
        if error_type or (status_code is not None and status_code >= 400):
            handle.span.set_status(Status(StatusCode.ERROR))
        context.detach(handle.token)
        handle.span.end()

    def shutdown(self) -> None:
        self.provider.shutdown()

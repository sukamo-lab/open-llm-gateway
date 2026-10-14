from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import __version__


_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id", default=None)

LOG_FIELDS = (
    "event_name", "http_method", "http_path", "http_status_code", "duration_ms",
    "tenant_id", "provider", "model", "upstream_model", "attempt", "outcome", "retry_delay_seconds",
)


def begin_request(request_id: str) -> tuple[contextvars.Token, contextvars.Token]:
    return _request_id.set(request_id), _tenant_id.set(None)


def bind_tenant(tenant_id: str) -> None:
    _tenant_id.set(tenant_id)


def end_request(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    _request_id.reset(tokens[0])
    _tenant_id.reset(tokens[1])


class JsonFormatter(logging.Formatter):
    """Stable, content-safe JSON log format suitable for common log collectors."""

    def __init__(self, include_exception_stacktraces: bool = False):
        super().__init__()
        self.include_exception_stacktraces = include_exception_stacktraces

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, Any] = {
            "@timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "log.level": record.levelname.lower(),
            "log.logger": record.name,
            "message": record.getMessage(),
            "service.name": "llm-gateway",
            "service.version": __version__,
        }
        request_id, tenant_id = _request_id.get(), _tenant_id.get()
        if request_id:
            event["request.id"] = request_id
        if tenant_id:
            event["tenant.id"] = tenant_id
        try:
            from opentelemetry import trace

            span_context = trace.get_current_span().get_span_context()
            if span_context.is_valid:
                event["trace.id"] = f"{span_context.trace_id:032x}"
                event["span.id"] = f"{span_context.span_id:016x}"
        except ImportError:
            pass
        for field in LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                name = "tenant.id" if field == "tenant_id" else field.replace("_", ".")
                event[name] = value
        if record.exc_info:
            event["error.type"] = record.exc_info[0].__name__
            if self.include_exception_stacktraces:
                event["error.stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str, include_exception_stacktraces: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(include_exception_stacktraces))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True

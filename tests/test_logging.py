import json
import logging
import sys

from llm_gateway.logging_config import JsonFormatter, begin_request, bind_tenant, end_request


def test_json_formatter_adds_correlation_and_ignores_unknown_extras():
    tokens = begin_request("request-123")
    bind_tenant("tenant-456")
    try:
        record = logging.LogRecord(
            "llm_gateway.test", logging.INFO, __file__, 1, "completed", (), None
        )
        record.event_name = "http.request"
        record.http_status_code = 200
        record.secret_value = "must-not-appear"
        event = json.loads(JsonFormatter().format(record))
    finally:
        end_request(tokens)

    assert event["request.id"] == "request-123"
    assert event["tenant.id"] == "tenant-456"
    assert event["event.name"] == "http.request"
    assert event["http.status.code"] == 200
    assert "must-not-appear" not in json.dumps(event)


def test_json_formatter_omits_stack_trace_by_default():
    try:
        raise RuntimeError("sensitive exception detail")
    except RuntimeError:
        record = logging.LogRecord(
            "llm_gateway.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    event = json.loads(JsonFormatter().format(record))
    assert event["error.type"] == "RuntimeError"
    assert "error.stack_trace" not in event
    assert "sensitive exception detail" not in json.dumps(event)

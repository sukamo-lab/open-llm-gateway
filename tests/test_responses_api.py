import pytest

from llm_gateway.config import ResponsesConfig
from llm_gateway.responses_api import ResponsesAdapterError, from_chat_response, to_chat_payload


def test_translates_text_instructions_tools_reasoning_and_json_schema():
    payload = to_chat_payload({
        "model": "reasoning",
        "metadata": {"request": "demo"},
        "instructions": "Be concise",
        "input": [{
            "type": "message", "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this"},
                {"type": "input_image", "image_url": "https://example.test/image.jpg", "detail": "low"},
            ],
        }],
        "max_output_tokens": 100,
        "reasoning": {"effort": "high"},
        "tools": [{
            "type": "function", "name": "lookup", "description": "Look up data",
            "parameters": {"type": "object"}, "strict": True,
        }],
        "tool_choice": {"type": "function", "name": "lookup"},
        "text": {"format": {
            "type": "json_schema", "name": "answer",
            "schema": {"type": "object"}, "strict": True,
        }},
    }, ResponsesConfig())

    assert payload["messages"][0] == {"role": "system", "content": "Be concise"}
    assert payload["_gateway_request_context"] == {
        "api": "responses", "metadata": {"request": "demo"},
    }
    assert payload["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.test/image.jpg", "detail": "low"},
    }
    assert payload["max_completion_tokens"] == 100
    assert payload["reasoning_effort"] == "high"
    assert payload["tools"][0]["function"]["name"] == "lookup"
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
    assert payload["response_format"]["json_schema"]["name"] == "answer"


def test_translates_function_call_history():
    payload = to_chat_payload({
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "result"},
        ],
    }, ResponsesConfig())
    assert payload["messages"][0]["tool_calls"][0]["id"] == "call_1"
    assert payload["messages"][1] == {
        "role": "tool", "tool_call_id": "call_1", "content": "result",
    }


@pytest.mark.parametrize("field", ["background", "store"])
def test_rejects_unsupported_stateful_modes(field):
    with pytest.raises(ResponsesAdapterError, match="not supported"):
        to_chat_payload({"input": "hello", field: True}, ResponsesConfig())


def test_rejects_streaming_and_unsupported_content():
    with pytest.raises(ResponsesAdapterError, match="streaming"):
        to_chat_payload({"input": "hello", "stream": True}, ResponsesConfig())
    with pytest.raises(ResponsesAdapterError, match="unsupported content"):
        to_chat_payload({
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_file"}]}],
        }, ResponsesConfig())
    with pytest.raises(ResponsesAdapterError, match="bounded strings"):
        to_chat_payload({"input": "hello", "metadata": {"key": 123}}, ResponsesConfig())


def test_translates_chat_response_message_tool_call_usage_and_gateway_metadata():
    result = from_chat_response({
        "id": "chatcmpl-123", "created": 100, "model": "fast",
        "choices": [{
            "finish_reason": "stop",
            "message": {
                "role": "assistant", "content": "Done",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }],
            },
        }],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        "gateway": {"provider": "free"},
    })
    assert result["id"] == "resp_123"
    assert result["status"] == "completed"
    assert result["output"][0]["content"][0]["text"] == "Done"
    assert result["output"][1]["type"] == "function_call"
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    assert result["gateway"] == {"provider": "free"}


def test_length_finish_reason_becomes_incomplete_response():
    result = from_chat_response({
        "id": "x", "model": "fast",
        "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
    })
    assert result["status"] == "incomplete"
    assert result["incomplete_details"] == {"reason": "max_output_tokens"}


def test_rejects_malformed_upstream_shapes_without_echoing_them():
    with pytest.raises(ResponsesAdapterError, match="choices must be an array"):
        from_chat_response({"choices": "private upstream body"})

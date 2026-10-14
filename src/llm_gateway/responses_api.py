from __future__ import annotations

import time
from typing import Any

from .config import ResponsesConfig


class ResponsesAdapterError(ValueError):
    pass


def to_chat_payload(payload: dict[str, Any], config: ResponsesConfig) -> dict[str, Any]:
    if payload.get("stream"):
        raise ResponsesAdapterError("streaming Responses API requests are not yet supported")
    if payload.get("background"):
        raise ResponsesAdapterError("background Responses API requests are not supported")
    if payload.get("store"):
        raise ResponsesAdapterError("hosted response storage is not supported")
    for field in ("previous_response_id", "conversation", "prompt"):
        if payload.get(field) is not None:
            raise ResponsesAdapterError(f"{field} is not supported")
    if "input" not in payload:
        raise ResponsesAdapterError("input is required")
    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or len(metadata) > config.max_metadata_entries:
            raise ResponsesAdapterError("metadata must be an object within the configured entry limit")
        if any(
            not isinstance(key, str) or len(key) > 64
            or not isinstance(value, str) or len(value) > 512
            for key, value in metadata.items()
        ):
            raise ResponsesAdapterError("metadata keys and values must be bounded strings")

    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise ResponsesAdapterError("instructions must be a string")
        messages.append({"role": "system", "content": instructions})
    input_value = payload["input"]
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        if len(input_value) > config.max_input_items:
            raise ResponsesAdapterError("input exceeds configured item limit")
        messages.extend(_input_item(item) for item in input_value)
    else:
        raise ResponsesAdapterError("input must be a string or array")

    result: dict[str, Any] = {
        "model": str(payload.get("model", "auto")),
        "messages": messages,
        "stream": False,
        # Participates in cache/idempotency fingerprints but is stripped before upstream I/O.
        "_gateway_request_context": {
            "api": "responses", "metadata": metadata or {},
        },
    }
    mappings = {
        "temperature": "temperature",
        "top_p": "top_p",
        "max_output_tokens": "max_completion_tokens",
        "parallel_tool_calls": "parallel_tool_calls",
        "user": "user",
        "service_tier": "service_tier",
    }
    for source, target in mappings.items():
        if payload.get(source) is not None:
            result[target] = payload[source]
    reasoning = payload.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, dict):
            raise ResponsesAdapterError("reasoning must be an object")
        if reasoning.get("effort") is not None:
            result["reasoning_effort"] = reasoning["effort"]
    if payload.get("tool_choice") is not None:
        result["tool_choice"] = _tool_choice(payload["tool_choice"])
    tools = payload.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or len(tools) > config.max_tools:
            raise ResponsesAdapterError("tools must be an array within the configured limit")
        result["tools"] = [_tool(tool) for tool in tools]
    text = payload.get("text")
    if text is not None:
        if not isinstance(text, dict):
            raise ResponsesAdapterError("text must be an object")
        if text.get("format") is not None:
            response_format = _response_format(text["format"])
            if response_format is not None:
                result["response_format"] = response_format
    return result


def _input_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ResponsesAdapterError("each input item must be an object")
    item_type = item.get("type", "message")
    if item_type == "message":
        role = item.get("role")
        if role not in {"user", "assistant", "system", "developer"}:
            raise ResponsesAdapterError("input message role is not supported")
        return {"role": role, "content": _input_content(item.get("content"))}
    if item_type == "function_call_output":
        call_id, output = item.get("call_id"), item.get("output")
        if not isinstance(call_id, str) or not isinstance(output, str):
            raise ResponsesAdapterError("function_call_output requires string call_id and output")
        return {"role": "tool", "tool_call_id": call_id, "content": output}
    if item_type == "function_call":
        call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
        if not all(isinstance(value, str) for value in (call_id, name, arguments)):
            raise ResponsesAdapterError("function_call requires string call_id, name, and arguments")
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": arguments},
            }],
        }
    raise ResponsesAdapterError(f"unsupported input item type: {item_type}")


def _input_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ResponsesAdapterError("message content must be a string or array")
    converted = []
    for part in content:
        if not isinstance(part, dict):
            raise ResponsesAdapterError("content parts must be objects")
        part_type = part.get("type")
        if part_type in {"input_text", "output_text"} and isinstance(part.get("text"), str):
            converted.append({"type": "text", "text": part["text"]})
        elif part_type == "input_image" and isinstance(part.get("image_url"), str):
            image = {"url": part["image_url"]}
            if part.get("detail") is not None:
                image["detail"] = part["detail"]
            converted.append({"type": "image_url", "image_url": image})
        else:
            raise ResponsesAdapterError(f"unsupported content part type: {part_type}")
    return converted


def _tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict) or tool.get("type") != "function":
        raise ResponsesAdapterError("only function tools are currently supported")
    name, parameters = tool.get("name"), tool.get("parameters", {})
    if not isinstance(name, str) or not isinstance(parameters, dict):
        raise ResponsesAdapterError("function tools require a string name and object parameters")
    function = {"name": name, "parameters": parameters}
    if tool.get("description") is not None:
        if not isinstance(tool["description"], str):
            raise ResponsesAdapterError("function tool description must be a string")
        function["description"] = tool["description"]
    if tool.get("strict") is not None:
        function["strict"] = bool(tool["strict"])
    return {"type": "function", "function": function}


def _tool_choice(choice: Any) -> Any:
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        return choice
    if isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
        return {"type": "function", "function": {"name": choice["name"]}}
    raise ResponsesAdapterError("tool_choice is not supported")


def _response_format(format_value: Any) -> dict[str, Any] | None:
    if not isinstance(format_value, dict):
        raise ResponsesAdapterError("text.format must be an object")
    format_type = format_value.get("type", "text")
    if format_type == "text":
        return None
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type == "json_schema":
        name, schema = format_value.get("name"), format_value.get("schema")
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise ResponsesAdapterError("json_schema format requires string name and object schema")
        value = {"name": name, "schema": schema}
        if format_value.get("description") is not None:
            value["description"] = format_value["description"]
        if format_value.get("strict") is not None:
            value["strict"] = bool(format_value["strict"])
        return {"type": "json_schema", "json_schema": value}
    raise ResponsesAdapterError(f"unsupported text format: {format_type}")


def from_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices") or []
    if not isinstance(choices, list):
        raise ResponsesAdapterError("upstream choices must be an array")
    if choices and not isinstance(choices[0], dict):
        raise ResponsesAdapterError("upstream choice must be an object")
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    if choice.get("message") is not None and not isinstance(choice.get("message"), dict):
        raise ResponsesAdapterError("upstream message must be an object")
    message = choice.get("message") or {}
    output: list[dict[str, Any]] = []
    content = _output_content(message.get("content"))
    if message.get("refusal"):
        content.append({"type": "refusal", "refusal": str(message["refusal"])})
    if content:
        output.append({
            "id": _prefixed_id("msg", body.get("id")),
            "type": "message", "status": "completed", "role": "assistant",
            "content": content,
        })
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise ResponsesAdapterError("upstream tool_calls must be an array")
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict) or tool_call.get("type") != "function":
            continue
        function = tool_call.get("function") or {}
        output.append({
            "id": _prefixed_id("fc", tool_call.get("id")),
            "type": "function_call", "status": "completed",
            "call_id": str(tool_call.get("id", "")),
            "name": str(function.get("name", "")),
            "arguments": str(function.get("arguments", "")),
        })
    finish_reason = choice.get("finish_reason")
    incomplete = finish_reason in {"length", "max_tokens"}
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        raise ResponsesAdapterError("upstream usage must be an object")
    try:
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
        created_at = int(body.get("created") or time.time())
    except (TypeError, ValueError) as exc:
        raise ResponsesAdapterError("upstream response contains invalid numeric fields") from exc
    result = {
        "id": _prefixed_id("resp", body.get("id")),
        "object": "response",
        "created_at": created_at,
        "status": "incomplete" if incomplete else "completed",
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        "model": body.get("model"),
        "output": output,
        "parallel_tool_calls": True,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "error": None,
        "metadata": {},
    }
    if "gateway" in body:
        result["gateway"] = body["gateway"]
    return result


def _output_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "output_text", "text": content, "annotations": []}]
    if not isinstance(content, list):
        return []
    result = []
    for part in content:
        if isinstance(part, str):
            result.append({"type": "output_text", "text": part, "annotations": []})
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            result.append({"type": "output_text", "text": part["text"], "annotations": []})
        elif isinstance(part, dict) and isinstance(part.get("refusal"), str):
            result.append({"type": "refusal", "refusal": part["refusal"]})
    return result


def _prefixed_id(prefix: str, value: Any) -> str:
    text = str(value or "gateway")
    for known in ("chatcmpl-", "resp_", "msg_", "fc_"):
        if text.startswith(known):
            text = text[len(known):]
            break
    return f"{prefix}_{text}"

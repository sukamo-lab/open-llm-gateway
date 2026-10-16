from __future__ import annotations

import base64
import binascii
from typing import Any

from .config import ImagesConfig


class ImagesValidationError(ValueError):
    pass


def validate_image_payload(payload: dict[str, Any], config: ImagesConfig) -> dict[str, Any]:
    supported = {
        "model", "prompt", "n", "quality", "response_format", "size", "style", "user", "provider",
        "routing_policy",
    }
    unknown = sorted(set(payload) - supported)
    if unknown:
        raise ImagesValidationError(f"unsupported image field: {unknown[0]}")
    result = dict(payload)
    prompt = result.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ImagesValidationError("prompt must be non-empty text")
    if len(prompt) > config.max_prompt_characters:
        raise ImagesValidationError("prompt exceeds configured character limit")
    model = result.get("model", "auto")
    if not isinstance(model, str) or not model:
        raise ImagesValidationError("model must be non-empty text")
    result["model"] = model
    n = result.get("n", 1)
    if type(n) is not int or not 1 <= n <= config.max_images:
        raise ImagesValidationError("n is outside the configured range")
    result["n"] = n
    response_format = result.get("response_format", "url")
    if response_format not in config.response_formats:
        raise ImagesValidationError("response_format is not enabled")
    result["response_format"] = response_format
    for field in ("quality", "size", "style", "user", "provider", "routing_policy"):
        if field in result and (not isinstance(result[field], str) or not result[field]):
            raise ImagesValidationError(f"{field} must be non-empty text")
    if len(result.get("routing_policy", "")) > 64:
        raise ImagesValidationError("routing_policy must be at most 64 characters")
    return result


def validate_image_response(body: Any, expected: int, response_format: str) -> bool:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return False
    data = body["data"]
    if len(data) != expected:
        return False
    key = "b64_json" if response_format == "b64_json" else "url"
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get(key), str) or not item[key]:
            return False
        if key == "url" and not item[key].startswith(("https://", "http://")):
            return False
        if key == "b64_json":
            try:
                base64.b64decode(item[key], validate=True)
            except (ValueError, binascii.Error):
                return False
    return True

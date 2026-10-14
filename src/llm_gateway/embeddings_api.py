from __future__ import annotations

from typing import Any

from .config import EmbeddingsConfig


class EmbeddingsValidationError(ValueError):
    pass


def validate_embeddings_payload(
    payload: dict[str, Any], config: EmbeddingsConfig,
) -> dict[str, Any]:
    allowed = {"model", "input", "encoding_format", "dimensions", "user", "provider"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise EmbeddingsValidationError(f"unsupported embeddings field: {unknown[0]}")
    if "input" not in payload:
        raise EmbeddingsValidationError("input is required")
    input_value = payload["input"]
    _validate_input(input_value, config)
    encoding = payload.get("encoding_format", "float")
    if encoding not in {"float", "base64"}:
        raise EmbeddingsValidationError("encoding_format must be float or base64")
    dimensions = payload.get("dimensions")
    if dimensions is not None and (
        type(dimensions) is not int or not 1 <= dimensions <= config.max_dimensions
    ):
        raise EmbeddingsValidationError("dimensions is outside the configured range")
    user = payload.get("user")
    if user is not None and (not isinstance(user, str) or len(user) > 256):
        raise EmbeddingsValidationError("user must be a string of at most 256 characters")
    provider = payload.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider):
        raise EmbeddingsValidationError("provider must be a non-empty string")
    model = payload.get("model", "auto")
    if not isinstance(model, str) or not model:
        raise EmbeddingsValidationError("model must be a non-empty string")
    result = {"model": model, "input": input_value, "encoding_format": encoding}
    for field in ("dimensions", "user", "provider"):
        if payload.get(field) is not None:
            result[field] = payload[field]
    return result


def _validate_input(value: Any, config: EmbeddingsConfig) -> None:
    inputs = value if isinstance(value, list) else [value]
    if not inputs:
        raise EmbeddingsValidationError("input must not be empty")
    token_batch = isinstance(value, list) and value and all(isinstance(item, list) for item in value)
    single_tokens = isinstance(value, list) and value and all(type(item) is int for item in value)
    if single_tokens:
        inputs = [value]
        token_batch = True
    if len(inputs) > config.max_inputs:
        raise EmbeddingsValidationError("input exceeds configured item limit")
    if token_batch:
        if not config.allow_token_arrays:
            raise EmbeddingsValidationError("token-array input is disabled")
        total = 0
        for tokens in inputs:
            if not isinstance(tokens, list) or not tokens or any(
                type(token) is not int or token < 0 for token in tokens
            ):
                raise EmbeddingsValidationError("token arrays must contain non-negative integers")
            total += len(tokens)
        if total > config.max_token_ids:
            raise EmbeddingsValidationError("token-array input exceeds configured token-id limit")
        return
    if not all(isinstance(item, str) and item for item in inputs):
        raise EmbeddingsValidationError("input must be text, text arrays, or token arrays")
    if sum(len(item) for item in inputs) > config.max_input_characters:
        raise EmbeddingsValidationError("input exceeds configured character limit")

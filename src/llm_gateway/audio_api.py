from __future__ import annotations

from typing import Any

from .config import AudioConfig


class AudioValidationError(ValueError):
    pass


def validate_speech_payload(payload: dict[str, Any], config: AudioConfig) -> dict[str, Any]:
    supported = {
        "model", "input", "voice", "response_format", "speed", "instructions", "provider",
        "routing_policy",
    }
    unknown = sorted(set(payload) - supported)
    if unknown:
        raise AudioValidationError(f"unsupported speech field: {unknown[0]}")
    result = dict(payload)
    for required in ("model", "input", "voice"):
        if not isinstance(result.get(required), str) or not result[required]:
            raise AudioValidationError(f"{required} must be non-empty text")
    if len(result["input"]) > config.max_input_characters:
        raise AudioValidationError("input exceeds configured character limit")
    response_format = result.get("response_format", "mp3")
    if response_format not in config.response_formats:
        raise AudioValidationError("response_format is not enabled")
    result["response_format"] = response_format
    speed = result.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.25 <= speed <= 4.0:
        raise AudioValidationError("speed must be between 0.25 and 4.0")
    result["speed"] = float(speed)
    if "instructions" in result and (
        not isinstance(result["instructions"], str) or len(result["instructions"]) > 4096
    ):
        raise AudioValidationError("instructions must be bounded text")
    if "provider" in result and not isinstance(result["provider"], str):
        raise AudioValidationError("provider must be text")
    if "routing_policy" in result and (
        not isinstance(result["routing_policy"], str)
        or not result["routing_policy"]
        or len(result["routing_policy"]) > 64
    ):
        raise AudioValidationError("routing_policy must be bounded non-empty text")
    return result


CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "application/octet-stream",
}

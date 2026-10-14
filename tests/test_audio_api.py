import pytest

from llm_gateway.audio_api import AudioValidationError, validate_speech_payload
from llm_gateway.config import AudioConfig


def test_speech_defaults_and_normalizes_speed():
    result = validate_speech_payload(
        {"model": "speak", "input": "Hello", "voice": "alloy"}, AudioConfig()
    )
    assert result["response_format"] == "mp3"
    assert result["speed"] == 1.0


@pytest.mark.parametrize("field", ["model", "input", "voice"])
def test_speech_requires_text_fields(field):
    payload = {"model": "speak", "input": "Hello", "voice": "alloy"}
    payload[field] = ""
    with pytest.raises(AudioValidationError, match=field):
        validate_speech_payload(payload, AudioConfig())


def test_speech_rejects_unknown_fields_limits_and_formats():
    base = {"model": "speak", "input": "Hello", "voice": "alloy"}
    with pytest.raises(AudioValidationError, match="unsupported"):
        validate_speech_payload({**base, "extra": True}, AudioConfig())
    with pytest.raises(AudioValidationError, match="character limit"):
        validate_speech_payload(base, AudioConfig(max_input_characters=4))
    with pytest.raises(AudioValidationError, match="not enabled"):
        validate_speech_payload({**base, "response_format": "wav"}, AudioConfig(response_formats={"mp3"}))
    with pytest.raises(AudioValidationError, match="speed"):
        validate_speech_payload({**base, "speed": 9}, AudioConfig())

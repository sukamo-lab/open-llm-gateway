import pytest

from llm_gateway.config import ImagesConfig
from llm_gateway.images_api import (
    ImagesValidationError,
    validate_image_payload,
    validate_image_response,
)


def test_image_request_defaults_and_bounds():
    result = validate_image_payload({"prompt": "A small lighthouse"}, ImagesConfig())
    assert result["model"] == "auto"
    assert result["n"] == 1
    assert result["response_format"] == "url"
    with pytest.raises(ImagesValidationError, match="configured range"):
        validate_image_payload({"prompt": "x", "n": 5}, ImagesConfig(max_images=4))
    with pytest.raises(ImagesValidationError, match="character limit"):
        validate_image_payload({"prompt": "long"}, ImagesConfig(max_prompt_characters=3))


def test_image_request_rejects_unknown_and_disabled_format():
    with pytest.raises(ImagesValidationError, match="unsupported"):
        validate_image_payload({"prompt": "x", "extra": True}, ImagesConfig())
    with pytest.raises(ImagesValidationError, match="not enabled"):
        validate_image_payload(
            {"prompt": "x", "response_format": "b64_json"},
            ImagesConfig(response_formats={"url"}),
        )


def test_image_response_requires_expected_shape_and_count():
    assert validate_image_response({"data": [{"url": "https://example.test/image"}]}, 1, "url")
    assert validate_image_response({"data": [{"b64_json": "aGVsbG8="}]}, 1, "b64_json")
    assert not validate_image_response({"data": []}, 1, "url")
    assert not validate_image_response({"data": [{"url": ""}]}, 1, "url")

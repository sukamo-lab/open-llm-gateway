import pytest

from llm_gateway.config import ModerationsAPIConfig
from llm_gateway.moderation import ModerationReport
from llm_gateway.moderations_api import (
    CATEGORY_NAMES,
    ModerationsValidationError,
    moderation_response,
    validate_moderations_payload,
)


def test_validates_single_and_batched_text_with_model_aliases():
    config = ModerationsAPIConfig()
    assert validate_moderations_payload({"input": "hello"}, config) == (
        "gateway-moderation-latest", ["hello"],
    )
    assert validate_moderations_payload({
        "model": "omni-moderation-latest", "input": ["one", "two"],
    }, config) == ("omni-moderation-latest", ["one", "two"])


@pytest.mark.parametrize("value", [None, "", [], ["ok", 1]])
def test_rejects_invalid_input(value):
    with pytest.raises(ModerationsValidationError):
        validate_moderations_payload({"input": value}, ModerationsAPIConfig())


def test_rejects_unknown_models_fields_and_bounds():
    with pytest.raises(ModerationsValidationError, match="not accepted"):
        validate_moderations_payload({"model": "unknown", "input": "hello"}, ModerationsAPIConfig())
    with pytest.raises(ModerationsValidationError, match="unsupported moderations field"):
        validate_moderations_payload({"input": "hello", "extra": True}, ModerationsAPIConfig())
    with pytest.raises(ModerationsValidationError, match="input count"):
        validate_moderations_payload({"input": ["one", "two"]}, ModerationsAPIConfig(max_inputs=1))


def test_response_is_openai_shaped_and_exposes_rule_semantics():
    response = moderation_response("gateway-moderation-latest", [
        ModerationReport(
            action="monitor", flagged=True, findings=2,
            categories=("self_harm", "violence"),
        ),
    ])
    result = response["results"][0]
    assert response["id"].startswith("modr-")
    assert result["flagged"] is True
    assert result["categories"]["self-harm"] is True
    assert result["categories"]["self-harm/instructions"] is True
    assert result["categories"]["violence"] is True
    assert set(result["category_scores"]) == set(CATEGORY_NAMES)
    assert result["gateway"]["score_semantics"] == "deterministic_rule_match"

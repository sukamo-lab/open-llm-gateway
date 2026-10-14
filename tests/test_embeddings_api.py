import pytest

from llm_gateway.config import EmbeddingsConfig
from llm_gateway.embeddings_api import EmbeddingsValidationError, validate_embeddings_payload


def test_validates_text_batches_and_optional_fields():
    result = validate_embeddings_payload({
        "model": "embed", "input": ["one", "two"],
        "encoding_format": "base64", "dimensions": 256, "user": "consumer",
    }, EmbeddingsConfig())
    assert result == {
        "model": "embed", "input": ["one", "two"],
        "encoding_format": "base64", "dimensions": 256, "user": "consumer",
    }


def test_validates_single_and_batched_token_arrays():
    assert validate_embeddings_payload({"input": [1, 2, 3]}, EmbeddingsConfig())["input"] == [1, 2, 3]
    assert validate_embeddings_payload({"input": [[1, 2], [3]]}, EmbeddingsConfig())["input"] == [[1, 2], [3]]
    with pytest.raises(EmbeddingsValidationError, match="disabled"):
        validate_embeddings_payload(
            {"input": [1, 2]}, EmbeddingsConfig(allow_token_arrays=False)
        )


@pytest.mark.parametrize("input_value", [None, "", [], ["ok", 1], [[1, -1]]])
def test_rejects_invalid_input_shapes(input_value):
    with pytest.raises(EmbeddingsValidationError):
        validate_embeddings_payload({"input": input_value}, EmbeddingsConfig())


def test_enforces_bounds_and_unknown_fields():
    with pytest.raises(EmbeddingsValidationError, match="item limit"):
        validate_embeddings_payload(
            {"input": ["a", "b"]}, EmbeddingsConfig(max_inputs=1)
        )
    with pytest.raises(EmbeddingsValidationError, match="character limit"):
        validate_embeddings_payload(
            {"input": "x" * 1025}, EmbeddingsConfig(max_input_characters=1024)
        )
    with pytest.raises(EmbeddingsValidationError, match="unsupported embeddings field"):
        validate_embeddings_payload({"input": "hello", "unknown": True}, EmbeddingsConfig())

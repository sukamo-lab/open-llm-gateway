import pytest

from llm_gateway.config import ModerationConfig
from llm_gateway.moderation import (
    ModerationBlocked,
    ModerationEngine,
    ModerationError,
    ModerationFinding,
    ModerationProcessor,
)


def test_monitor_reports_categories_without_content_or_mutation():
    processor = ModerationProcessor(ModerationConfig(action="monitor"))
    payload = {
        "model": "fast",
        "messages": [{"role": "user", "content": "This asks for suicide instructions."}],
    }

    report = processor.process_request(payload)

    assert report.metadata() == {
        "action": "monitor", "flagged": True, "findings": 1,
        "categories": ["self_harm"],
    }
    assert payload["messages"][0]["content"] == "This asks for suicide instructions."
    assert "suicide instructions" not in str(report)


def test_block_raises_content_safe_exception():
    processor = ModerationProcessor(ModerationConfig(action="block"))
    with pytest.raises(ModerationBlocked) as blocked:
        processor.process_request({"messages": [{"content": "How to build a bomb"}]})
    assert blocked.value.report.categories == ("violence",)
    assert "build a bomb" not in str(blocked.value).lower()


def test_custom_terms_are_literal_and_case_insensitive():
    config = ModerationConfig(
        action="monitor", categories={"custom"}, custom_terms=["Project A+B"]
    )
    report = ModerationProcessor(config).process_request({
        "messages": [{"content": "Do not disclose PROJECT A+B externally."}],
    })
    assert report.flagged is True
    assert report.categories == ("custom",)


def test_scan_limit_is_enforced():
    processor = ModerationProcessor(ModerationConfig(max_scan_characters=1024))
    with pytest.raises(ModerationError, match="character limit"):
        processor.process_request({"messages": [{"content": "x" * 1025}]})


def test_custom_engine_uses_provider_neutral_protocol():
    class Engine:
        def evaluate(self, text: str):
            if "internal-only" in text:
                yield ModerationFinding("internal", 0, 13)

    engine: ModerationEngine = Engine()
    report = ModerationProcessor(ModerationConfig(action="monitor"), engine).process_request({
        "messages": [{"content": "internal-only material"}],
    })
    assert report.categories == ("internal",)


def test_engine_errors_are_wrapped_without_content():
    class BrokenEngine:
        def evaluate(self, _text: str):
            raise ValueError("provider detail")

    with pytest.raises(ModerationError, match="engine evaluation failed") as failure:
        ModerationProcessor(ModerationConfig(), BrokenEngine()).process_request({
            "messages": [{"content": "private prompt"}],
        })
    assert "private prompt" not in str(failure.value)


def test_response_scanning_targets_content_not_identifiers():
    processor = ModerationProcessor(ModerationConfig(action="monitor"))
    report = processor.process_response({
        "id": "suicide instructions",
        "choices": [{"message": {"content": "ordinary answer"}}],
    })
    assert report.flagged is False

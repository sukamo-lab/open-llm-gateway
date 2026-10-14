import pytest

from llm_gateway.config import PIIConfig
from llm_gateway.pii import PIIDetector, PIIMatch, PIIBlocked, PIIProcessor, PIIScanError


def test_redacts_supported_pii_without_changing_routing_fields():
    processor = PIIProcessor(PIIConfig(
        detectors={"email", "phone", "credit_card", "us_ssn", "ipv4"},
    ))
    payload = {
        "model": "email@example.com",
        "messages": [{
            "role": "user",
            "content": (
                "Email jane@example.com, call +1 415-555-2671, SSN 123-45-6789, "
                "card 4111 1111 1111 1111, IP 192.168.1.10."
            ),
        }],
    }

    result, report = processor.process_request(payload)

    content = result["messages"][0]["content"]
    assert "jane@example.com" not in content
    assert "415-555-2671" not in content
    assert "123-45-6789" not in content
    assert "4111 1111 1111 1111" not in content
    assert "192.168.1.10" not in content
    assert result["model"] == "email@example.com"
    assert report.matches == 5
    assert report.redactions == 5
    assert report.types == ("credit_card", "email", "ipv4", "phone", "us_ssn")
    assert payload["messages"][0]["content"].startswith("Email jane@example.com")


def test_invalid_card_and_ipv4_are_not_redacted():
    processor = PIIProcessor(PIIConfig(detectors={"credit_card", "ipv4"}))
    result, report = processor.process_request({
        "messages": [{"role": "user", "content": "9999 9999 9999 9999 and 999.1.1.1"}],
    })
    assert report.matches == 0
    assert result["messages"][0]["content"] == "9999 9999 9999 9999 and 999.1.1.1"


def test_detect_preserves_content_and_block_never_returns_content():
    payload = {"messages": [{"role": "user", "content": "jane@example.com"}]}
    detected, report = PIIProcessor(PIIConfig(action="detect", detectors={"email"})).process_request(payload)
    assert detected == payload
    assert report.metadata() == {
        "action": "detect", "matches": 1, "types": ["email"], "redactions": 0,
    }

    with pytest.raises(PIIBlocked) as blocked:
        PIIProcessor(PIIConfig(action="block", detectors={"email"})).process_request(payload)
    assert blocked.value.report.types == ("email",)
    assert "jane@example.com" not in str(blocked.value)


def test_scan_limit_is_enforced_before_provider_processing():
    processor = PIIProcessor(PIIConfig(max_scan_characters=1024, detectors={"email"}))
    with pytest.raises(PIIScanError, match="character limit"):
        processor.process_request({"messages": [{"content": "x" * 1025}]})


def test_custom_detector_uses_extension_protocol():
    class AccountDetector:
        kind = "account"

        def find(self, text: str):
            start = text.find("ACCT-")
            if start >= 0:
                yield PIIMatch(self.kind, start, start + 9)

    detector: PIIDetector = AccountDetector()
    processor = PIIProcessor(PIIConfig(detectors={"email"}), detectors=[detector])
    result, report = processor.process_request({"messages": [{"content": "Use ACCT-1234"}]})
    assert result["messages"][0]["content"] == "Use [REDACTED:account]"
    assert report.types == ("account",)


def test_response_scanning_targets_model_content_not_identifiers():
    processor = PIIProcessor(PIIConfig(detectors={"email"}))
    response, report = processor.process_response({
        "id": "jane@example.com",
        "choices": [{"message": {"role": "assistant", "content": "Contact jane@example.com"}}],
    })
    assert response["id"] == "jane@example.com"
    assert response["choices"][0]["message"]["content"] == "Contact [REDACTED:email]"
    assert report.matches == 1

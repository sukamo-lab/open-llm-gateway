from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any, Iterable, Protocol

from .config import PIIConfig


class PIIScanError(RuntimeError):
    pass


class PIIBlocked(PIIScanError):
    def __init__(self, report: "PIIReport"):
        super().__init__("request blocked by PII policy")
        self.report = report


@dataclass(frozen=True)
class PIIMatch:
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class PIIReport:
    action: str
    matches: int
    types: tuple[str, ...]
    redactions: int

    def metadata(self) -> dict[str, object]:
        return {
            "action": self.action,
            "matches": self.matches,
            "types": list(self.types),
            "redactions": self.redactions,
        }


class PIIDetector(Protocol):
    """Extension boundary for local or externally packaged PII detectors."""

    kind: str

    def find(self, text: str) -> Iterable[PIIMatch]: ...


class RegexDetector:
    def __init__(self, kind: str, pattern: str):
        self.kind = kind
        self._pattern = re.compile(pattern, re.IGNORECASE)

    def find(self, text: str) -> Iterable[PIIMatch]:
        for match in self._pattern.finditer(text):
            yield PIIMatch(self.kind, match.start(), match.end())


class IPv4Detector(RegexDetector):
    def __init__(self):
        # Permit ordinary sentence punctuation after an address while still rejecting
        # a fifth dotted numeric component.
        super().__init__("ipv4", r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)")

    def find(self, text: str) -> Iterable[PIIMatch]:
        for match in super().find(text):
            if all(int(part) <= 255 for part in text[match.start:match.end].split(".")):
                yield match


class CreditCardDetector(RegexDetector):
    def __init__(self):
        super().__init__("credit_card", r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

    def find(self, text: str) -> Iterable[PIIMatch]:
        for match in super().find(text):
            candidate = text[match.start:match.end]
            digit_positions = [index for index, value in enumerate(candidate) if value.isdigit()]
            # The broad candidate can absorb a following number separated by whitespace.
            # Select the longest Luhn-valid prefix so adjacent PII remains independently detectable.
            for length in range(min(19, len(digit_positions)), 12, -1):
                digits = [int(candidate[index]) for index in digit_positions[:length]]
                checksum = 0
                parity = len(digits) % 2
                for index, digit in enumerate(digits):
                    value = digit * 2 if index % 2 == parity else digit
                    checksum += value - 9 if value > 9 else value
                if checksum % 10 == 0:
                    yield PIIMatch(
                        self.kind, match.start,
                        match.start + digit_positions[length - 1] + 1,
                    )
                    break


BUILTIN_DETECTORS: dict[str, PIIDetector] = {
    "credit_card": CreditCardDetector(),
    "email": RegexDetector(
        "email", r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}(?![\w.-])"
    ),
    "phone": RegexDetector(
        "phone", r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)"
    ),
    "us_ssn": RegexDetector("us_ssn", r"(?<!\d)(?!000|666|9\d\d)\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}(?!\d)"),
    "ipv4": IPv4Detector(),
}


class PIIProcessor:
    REQUEST_TEXT_KEYS = frozenset({"content", "text", "arguments", "description", "input"})
    RESPONSE_TEXT_KEYS = frozenset({
        "content", "text", "arguments", "refusal", "reasoning", "output_text"
    })

    def __init__(self, config: PIIConfig, detectors: Iterable[PIIDetector] | None = None):
        self.config = config
        selected = detectors if detectors is not None else (
            BUILTIN_DETECTORS[name] for name in BUILTIN_DETECTORS if name in config.detectors
        )
        self.detectors = tuple(selected)

    def process_request(self, payload: dict[str, Any]) -> tuple[dict[str, Any], PIIReport]:
        return self._process(payload, self.REQUEST_TEXT_KEYS)

    def process_response(self, payload: dict[str, Any]) -> tuple[dict[str, Any], PIIReport]:
        return self._process(payload, self.RESPONSE_TEXT_KEYS)

    def _process(
        self, payload: dict[str, Any], text_keys: frozenset[str]
    ) -> tuple[dict[str, Any], PIIReport]:
        scanned = 0
        counts: Counter[str] = Counter()

        def visit(value: Any, key: str | None = None) -> Any:
            nonlocal scanned
            if isinstance(value, dict):
                return {item_key: visit(item, item_key) for item_key, item in value.items()}
            if isinstance(value, list):
                return [visit(item, key) for item in value]
            if not isinstance(value, str) or key not in text_keys:
                return value
            scanned += len(value)
            if scanned > self.config.max_scan_characters:
                raise PIIScanError("PII scan character limit exceeded")
            matches = self._matches(value)
            counts.update(match.kind for match in matches)
            if self.config.action != "redact" or not matches:
                return value
            result = value
            for match in reversed(matches):
                replacement = self.config.replacement.format(type=match.kind)
                result = result[:match.start] + replacement + result[match.end:]
            return result

        result = visit(payload)
        total = sum(counts.values())
        report = PIIReport(
            self.config.action, total, tuple(sorted(counts)),
            total if self.config.action == "redact" else 0,
        )
        if total and self.config.action == "block":
            raise PIIBlocked(report)
        return result, report

    def _matches(self, text: str) -> list[PIIMatch]:
        try:
            candidates = sorted(
                (match for detector in self.detectors for match in detector.find(text)),
                key=lambda match: (match.start, -(match.end - match.start)),
            )
        except Exception as exc:
            raise PIIScanError("PII detector evaluation failed") from exc
        accepted: list[PIIMatch] = []
        for match in candidates:
            if not accepted or match.start >= accepted[-1].end:
                accepted.append(match)
        return accepted

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any, Iterable, Protocol

from .config import ModerationConfig


class ModerationError(RuntimeError):
    pass


class ModerationBlocked(ModerationError):
    def __init__(self, report: "ModerationReport"):
        super().__init__("content blocked by moderation policy")
        self.report = report


@dataclass(frozen=True)
class ModerationFinding:
    category: str
    start: int
    end: int


@dataclass(frozen=True)
class ModerationReport:
    action: str
    flagged: bool
    findings: int
    categories: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "action": self.action,
            "flagged": self.flagged,
            "findings": self.findings,
            "categories": list(self.categories),
        }


class ModerationEngine(Protocol):
    """Provider-neutral extension boundary for moderation engines."""

    def evaluate(self, text: str) -> Iterable[ModerationFinding]: ...


DEFAULT_TERMS: dict[str, tuple[str, ...]] = {
    "hate": ("ethnic cleansing", "racial supremacy", "exterminate an ethnic group"),
    "harassment": ("dox this person", "publish their home address", "threaten this person"),
    "self_harm": ("kill myself", "end my life", "suicide instructions", "self-harm instructions"),
    "sexual": ("explicit sexual content", "sexual exploitation", "non-consensual sexual"),
    "violence": ("how to murder", "how to build a bomb", "graphic dismemberment", "violent attack instructions"),
}


class BuiltinModerationEngine:
    """Small deterministic baseline intended for local policy, testing, and custom terms."""

    def __init__(self, config: ModerationConfig):
        terms: list[tuple[str, str]] = []
        for category, category_terms in DEFAULT_TERMS.items():
            if category in config.categories:
                terms.extend((category, term) for term in category_terms)
        if "custom" in config.categories:
            terms.extend(("custom", term) for term in config.custom_terms)
        self._patterns = tuple(
            (category, re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE))
            for category, term in terms
        )

    def evaluate(self, text: str) -> Iterable[ModerationFinding]:
        for category, pattern in self._patterns:
            for match in pattern.finditer(text):
                yield ModerationFinding(category, match.start(), match.end())


class ModerationProcessor:
    REQUEST_TEXT_KEYS = frozenset({"content", "text", "arguments", "description", "input"})
    RESPONSE_TEXT_KEYS = frozenset({
        "content", "text", "arguments", "refusal", "reasoning", "output_text"
    })

    def __init__(self, config: ModerationConfig, engine: ModerationEngine | None = None):
        self.config = config
        self.engine = engine if engine is not None else BuiltinModerationEngine(config)

    def process_request(self, payload: dict[str, Any]) -> ModerationReport:
        return self._process(payload, self.REQUEST_TEXT_KEYS)

    def process_response(self, payload: dict[str, Any]) -> ModerationReport:
        return self._process(payload, self.RESPONSE_TEXT_KEYS)

    def _process(self, payload: dict[str, Any], text_keys: frozenset[str]) -> ModerationReport:
        scanned = 0
        counts: Counter[str] = Counter()

        def visit(value: Any, key: str | None = None) -> None:
            nonlocal scanned
            if isinstance(value, dict):
                for item_key, item in value.items():
                    visit(item, item_key)
                return
            if isinstance(value, list):
                for item in value:
                    visit(item, key)
                return
            if not isinstance(value, str) or key not in text_keys:
                return
            scanned += len(value)
            if scanned > self.config.max_scan_characters:
                raise ModerationError("moderation scan character limit exceeded")
            try:
                counts.update(finding.category for finding in self.engine.evaluate(value))
            except Exception as exc:
                raise ModerationError("moderation engine evaluation failed") from exc

        visit(payload)
        report = ModerationReport(
            action=self.config.action,
            flagged=bool(counts),
            findings=sum(counts.values()),
            categories=tuple(sorted(counts)),
        )
        if report.flagged and self.config.action == "block":
            raise ModerationBlocked(report)
        return report

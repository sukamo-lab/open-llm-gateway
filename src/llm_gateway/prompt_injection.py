from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Protocol

from .config import PromptInjectionConfig


class PromptInjectionError(RuntimeError):
    pass


class PromptInjectionBlocked(PromptInjectionError):
    def __init__(self, report: "PromptInjectionReport"):
        super().__init__("request blocked by prompt-injection policy")
        self.report = report


@dataclass(frozen=True)
class InjectionSignal:
    name: str
    weight: float


@dataclass(frozen=True)
class PromptInjectionReport:
    action: str
    score: float
    threshold: float
    risky: bool
    matches: int
    signals: tuple[str, ...]

    @property
    def route_required(self) -> bool:
        return self.risky and self.action == "route"

    def metadata(self) -> dict[str, object]:
        return {
            "action": self.action,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "risky": self.risky,
            "matches": self.matches,
            "signals": list(self.signals),
            "route_required": self.route_required,
        }


class PromptInjectionEngine(Protocol):
    """Provider-neutral extension boundary for prompt-injection risk engines."""

    def analyze(self, text: str) -> Iterable[InjectionSignal]: ...


SIGNAL_TERMS: dict[str, tuple[float, tuple[str, ...]]] = {
    "instruction_override": (
        0.55,
        ("ignore previous instructions", "disregard all prior instructions", "override system instructions"),
    ),
    "secret_exfiltration": (
        0.65,
        ("reveal the system prompt", "show hidden instructions", "print your api key"),
    ),
    "role_override": (
        0.30,
        ("you are now the system", "act as the system message", "enter developer mode"),
    ),
    "tool_manipulation": (
        0.40,
        ("call the shell tool", "execute this command", "invoke the admin tool"),
    ),
    "encoding_evasion": (
        0.25,
        ("decode this base64 and follow it", "decode rot13 and execute", "use unicode to bypass"),
    ),
}


class BuiltinPromptInjectionEngine:
    """Deterministic heuristic baseline; it is not a security boundary or classifier."""

    def __init__(self, config: PromptInjectionConfig):
        patterns: list[tuple[str, float, re.Pattern[str]]] = []
        for signal, (weight, terms) in SIGNAL_TERMS.items():
            if signal not in config.signals:
                continue
            patterns.extend(
                (signal, weight, re.compile(re.escape(term), re.IGNORECASE))
                for term in terms
            )
        self._patterns = tuple(patterns)

    def analyze(self, text: str) -> Iterable[InjectionSignal]:
        for signal, weight, pattern in self._patterns:
            for _match in pattern.finditer(text):
                yield InjectionSignal(signal, weight)


class PromptInjectionProcessor:
    TEXT_KEYS = frozenset({"content", "text", "arguments", "input"})

    def __init__(
        self, config: PromptInjectionConfig,
        engine: PromptInjectionEngine | None = None,
    ):
        self.config = config
        self.engine = engine if engine is not None else BuiltinPromptInjectionEngine(config)

    def process(self, payload: dict[str, Any]) -> PromptInjectionReport:
        scanned = 0
        findings: list[InjectionSignal] = []

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
            if not isinstance(value, str) or key not in self.TEXT_KEYS:
                return
            scanned += len(value)
            if scanned > self.config.max_scan_characters:
                raise PromptInjectionError("prompt-injection scan character limit exceeded")
            try:
                findings.extend(self.engine.analyze(value))
            except Exception as exc:
                raise PromptInjectionError("prompt-injection engine evaluation failed") from exc

        messages = payload.get("messages", [])
        if isinstance(messages, list):
            for message in messages:
                if (
                    isinstance(message, dict)
                    and message.get("role") in self.config.scan_roles
                ):
                    visit(message)
        weights_by_signal: dict[str, float] = {}
        for finding in findings:
            if (
                not isinstance(finding, InjectionSignal)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", finding.name)
                or not math.isfinite(finding.weight)
            ):
                raise PromptInjectionError("prompt-injection engine returned an invalid signal")
            weights_by_signal[finding.name] = max(
                weights_by_signal.get(finding.name, 0), min(1, max(0, finding.weight))
            )
        score = 1 - math.prod(1 - weight for weight in weights_by_signal.values())
        report = PromptInjectionReport(
            action=self.config.action,
            score=score,
            threshold=self.config.threshold,
            risky=score >= self.config.threshold,
            matches=len(findings),
            signals=tuple(sorted(weights_by_signal)),
        )
        if report.risky and self.config.action == "block":
            raise PromptInjectionBlocked(report)
        return report

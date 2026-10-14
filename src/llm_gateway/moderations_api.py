from __future__ import annotations

import uuid
from typing import Any

from .config import ModerationsAPIConfig
from .moderation import ModerationReport


class ModerationsValidationError(ValueError):
    pass


CATEGORY_NAMES = (
    "harassment", "harassment/threatening", "hate", "hate/threatening",
    "illicit", "illicit/violent", "self-harm", "self-harm/intent",
    "self-harm/instructions", "sexual", "sexual/minors", "violence", "violence/graphic",
)


def validate_moderations_payload(
    payload: dict[str, Any], config: ModerationsAPIConfig,
) -> tuple[str, list[str]]:
    unknown = sorted(set(payload) - {"input", "model"})
    if unknown:
        raise ModerationsValidationError(f"unsupported moderations field: {unknown[0]}")
    if "input" not in payload:
        raise ModerationsValidationError("input is required")
    input_value = payload["input"]
    inputs = input_value if isinstance(input_value, list) else [input_value]
    if not inputs or len(inputs) > config.max_inputs:
        raise ModerationsValidationError("input count is outside the configured range")
    if not all(isinstance(item, str) and item for item in inputs):
        raise ModerationsValidationError("input must be non-empty text or an array of non-empty text")
    if sum(len(item) for item in inputs) > config.max_input_characters:
        raise ModerationsValidationError("input exceeds configured character limit")
    model = payload.get("model", config.default_model)
    if not isinstance(model, str) or model not in config.accepted_models:
        raise ModerationsValidationError("requested moderation model is not accepted")
    return model, inputs


def moderation_response(model: str, reports: list[ModerationReport]) -> dict[str, Any]:
    return {
        "id": f"modr-{uuid.uuid4().hex}",
        "model": model,
        "results": [_result(report) for report in reports],
    }


def _result(report: ModerationReport) -> dict[str, Any]:
    categories = {name: False for name in CATEGORY_NAMES}
    mapping = {
        "harassment": ("harassment",),
        "hate": ("hate",),
        "self_harm": ("self-harm", "self-harm/instructions"),
        "sexual": ("sexual",),
        "violence": ("violence",),
    }
    for signal in report.categories:
        for category in mapping.get(signal, ()):
            categories[category] = True
    return {
        "flagged": report.flagged,
        "categories": categories,
        "category_scores": {
            category: 1.0 if flagged else 0.0
            for category, flagged in categories.items()
        },
        "category_applied_input_types": {
            category: ["text"] for category in categories
        },
        "gateway": {
            "engine": "builtin-rules-v1",
            "signals": list(report.categories),
            "finding_count": report.findings,
            "score_semantics": "deterministic_rule_match",
        },
    }

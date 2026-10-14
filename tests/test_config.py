from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_gateway.config import GatewayConfig, load_config


@pytest.mark.parametrize("buckets", ([1, 0.5], [0.5, 0.5], [0, 1], [1, float("inf")], []))
def test_observability_latency_buckets_must_be_increasing_positive(tmp_path, buckets):
    with pytest.raises(ValidationError, match="unique, increasing positive"):
        GatewayConfig.model_validate({
            "database": str(tmp_path / "usage.db"),
            "observability": {"http_latency_buckets_seconds": buckets},
        })


def test_unknown_configuration_key_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "database: ./db.sqlite\nunknown: true\nproviders:\n"
        "  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_value: key\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_config(path)


def test_includes_merge_and_paths_resolve_from_config(tmp_path: Path):
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "one.yaml").write_text(
        "providers:\n  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_value: key\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )
    root = tmp_path / "config.yaml"
    root.write_text("include: [providers/*.yaml]\ndatabase: ./data/db.sqlite\n", encoding="utf-8")
    config = load_config(root)
    assert config.providers[0].name == "p"
    assert config.database == str((tmp_path / "data/db.sqlite").resolve())


def test_placeholder_provider_key_is_not_configured():
    config = GatewayConfig.model_validate({
        "providers": [{
            "name": "p", "base_url": "https://example.test/v1",
            "api_key_value": "PASTE_YOUR_KEY_HERE",
            "models": [{"name": "fast", "upstream_model": "model"}],
        }]
    })
    assert config.providers[0].api_key() is None


def test_idempotency_mode_defaults_and_key_length_validation():
    provider = {
        "name": "p", "base_url": "https://example.test/v1", "api_key_value": "key",
        "models": [{"name": "fast", "upstream_model": "model"}],
    }
    assert GatewayConfig.model_validate({"providers": [provider]}).features.idempotency is False
    assert GatewayConfig.model_validate({"mode": "multi", "providers": [provider]}).features.idempotency is True
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({
            "idempotency": {"min_key_length": 100, "max_key_length": 20},
            "providers": [provider],
        })


def test_exact_cache_is_opt_in_and_redis_requires_url(monkeypatch):
    monkeypatch.delenv("TEST_REDIS_URL", raising=False)
    provider = {
        "name": "p", "base_url": "https://example.test/v1", "api_key_value": "key",
        "models": [{"name": "fast", "upstream_model": "model"}],
    }
    config = GatewayConfig.model_validate({"mode": "multi", "providers": [provider]})
    assert config.features.exact_cache is False
    with pytest.raises(ValidationError, match="TEST_REDIS_URL"):
        GatewayConfig.model_validate({
            "features": {"exact_cache": True},
            "cache": {"backend": "redis", "redis_url_env": "TEST_REDIS_URL"},
            "providers": [provider],
        })


def test_tracing_is_opt_in_and_batch_must_fit_queue():
    provider = {
        "name": "p", "base_url": "https://example.test/v1", "api_key_value": "key",
        "models": [{"name": "fast", "upstream_model": "model"}],
    }
    assert GatewayConfig.model_validate({"mode": "multi", "providers": [provider]}).features.tracing is False
    with pytest.raises(ValidationError, match="max_export_batch_size"):
        GatewayConfig.model_validate({
            "telemetry": {"max_queue_size": 10, "max_export_batch_size": 11},
            "providers": [provider],
        })


def test_pii_is_opt_in_and_replacement_is_strictly_validated():
    provider = {
        "name": "p", "base_url": "https://example.test/v1", "api_key_value": "key",
        "models": [{"name": "fast", "upstream_model": "model"}],
    }
    assert GatewayConfig.model_validate({"mode": "single", "providers": [provider]}).features.pii_detection is False
    assert GatewayConfig.model_validate({"mode": "multi", "providers": [provider]}).features.pii_detection is False
    with pytest.raises(ValidationError, match="replacement supports only"):
        GatewayConfig.model_validate({
            "pii": {"replacement": "[REDACTED:{value}]"},
            "providers": [provider],
        })
    config = GatewayConfig.model_validate({
        "features": {"pii_detection": True},
        "pii": {"scan": "both", "failure_mode": "open", "streaming_output": "bypass"},
        "providers": [provider],
    })
    warnings = [message for severity, message in config.diagnostics() if severity == "WARN"]
    assert any("scanner failures" in message for message in warnings)
    assert any("streaming responses bypass" in message for message in warnings)


def test_moderation_is_opt_in_and_custom_terms_are_strictly_validated():
    provider = {
        "name": "p", "base_url": "https://example.test/v1", "api_key_value": "key",
        "models": [{"name": "fast", "upstream_model": "model"}],
    }
    assert GatewayConfig.model_validate({"mode": "single", "providers": [provider]}).features.moderation is False
    assert GatewayConfig.model_validate({"mode": "multi", "providers": [provider]}).features.moderation is False
    with pytest.raises(ValidationError, match="require the custom category"):
        GatewayConfig.model_validate({
            "moderation": {"custom_terms": ["private phrase"]},
            "providers": [provider],
        })
    with pytest.raises(ValidationError, match="unique ignoring case"):
        GatewayConfig.model_validate({
            "moderation": {"categories": ["custom"], "custom_terms": ["Term", " term "]},
            "providers": [provider],
        })
    config = GatewayConfig.model_validate({
        "features": {"moderation": True},
        "moderation": {"scan": "both", "failure_mode": "open", "streaming_output": "bypass"},
        "providers": [provider],
    })
    warnings = [message for severity, message in config.diagnostics() if severity == "WARN"]
    assert any("moderation engine failures" in message for message in warnings)
    assert any("output moderation" in message for message in warnings)


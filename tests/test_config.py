from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_gateway.config import GatewayConfig, load_config


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


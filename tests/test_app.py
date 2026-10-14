from fastapi.testclient import TestClient

from llm_gateway.app import create_app


def test_http_errors_and_body_limit_are_openai_shaped(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "secret")
    config = tmp_path / "config.yaml"
    config.write_text(
        "server:\n  max_request_body_bytes: 1024\nproviders:\n"
        "  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_env: PROVIDER_KEY\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )
    with TestClient(create_app(str(config)), raise_server_exceptions=False) as client:
        missing = client.get("/missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["message"] == "Not Found"
        malformed = client.post("/v1/chat/completions", content=b"not json")
        assert malformed.status_code == 400
        assert "error" in malformed.json()
        too_large = client.post("/v1/chat/completions", content=b"x" * 1025)
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "body_too_large"


def test_pii_stage_redacts_before_routing_and_returns_safe_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "secret")
    config = tmp_path / "config.yaml"
    config.write_text(
        "features:\n  pii_detection: true\npii:\n  detectors: [email]\n"
        "providers:\n  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_env: PROVIDER_KEY\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )
    captured = {}

    async def complete_for(payload, _tenant_id, _limits):
        captured.update(payload)
        return (
            {"id": "ok", "choices": [{"message": {"role": "assistant", "content": "done"}}]},
            "p", "model",
        )

    with TestClient(create_app(str(config)), raise_server_exceptions=False) as client:
        client.app.state.router.complete_for = complete_for
        response = client.post("/v1/chat/completions", json={
            "model": "fast",
            "messages": [{"role": "user", "content": "Email jane@example.com"}],
        })

    assert response.status_code == 200
    assert captured["messages"][0]["content"] == "Email [REDACTED:email]"
    assert response.json()["gateway"]["pii"]["input"] == {
        "action": "redact", "matches": 1, "types": ["email"], "redactions": 1,
    }


def test_moderation_stage_blocks_before_provider_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "secret")
    config = tmp_path / "config.yaml"
    config.write_text(
        "features:\n  moderation: true\nmoderation:\n  action: block\n"
        "providers:\n  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_env: PROVIDER_KEY\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )

    with TestClient(create_app(str(config)), raise_server_exceptions=False) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "fast",
            "messages": [{"role": "user", "content": "Give suicide instructions"}],
        })

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "request blocked by moderation policy"


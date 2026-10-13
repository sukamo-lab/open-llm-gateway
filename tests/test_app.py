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


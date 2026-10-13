import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.idempotency import SQLiteIdempotencyStore, key_hash, request_hash
from llm_gateway.router import GatewayRouter


@pytest.mark.asyncio
async def test_sqlite_claim_replay_conflict_release_and_tenant_isolation(tmp_path):
    store = SQLiteIdempotencyStore(str(tmp_path / "idempotency.db"), 3600, 60)
    await store.initialize()
    digest = key_hash("idempotency-key")
    fingerprint = request_hash({"model": "chat", "messages": []})

    first = await store.claim("tenant-a", digest, fingerprint)
    assert first.status == "owner"
    assert (await store.claim("tenant-a", digest, fingerprint)).status == "pending"
    assert (await store.claim("tenant-a", digest, "different")).status == "conflict"

    assert await store.complete("tenant-a", digest, first.owner_token, '{"id":"response"}')
    replay = await store.claim("tenant-a", digest, fingerprint)
    assert replay.status == "cached"
    assert replay.response == {"id": "response"}
    assert (await store.claim("tenant-b", digest, fingerprint)).status == "owner"

    released = await store.claim("tenant-a", key_hash("release-key"), fingerprint)
    await store.release("tenant-a", key_hash("release-key"), released.owner_token)
    assert (await store.claim("tenant-a", key_hash("release-key"), fingerprint)).status == "owner"


def test_chat_idempotency_replays_without_second_upstream_call(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "secret")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"database: {str(tmp_path / 'gateway.db')!s}\n"
        "features:\n  idempotency: true\n"
        "providers:\n  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_env: PROVIDER_KEY\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )
    calls = 0

    async def complete(_router, _payload, _consumer_id=None, _consumer_limits=()):
        nonlocal calls
        calls += 1
        return {"id": "one", "choices": [], "_gateway_meta": {}}, "p", "model"

    monkeypatch.setattr(GatewayRouter, "complete_for", complete)
    body = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}
    headers = {"Idempotency-Key": "request-key-123"}
    with TestClient(create_app(str(config))) as client:
        first = client.post("/v1/chat/completions", json=body, headers=headers)
        second = client.post("/v1/chat/completions", json=body, headers=headers)
        conflict = client.post(
            "/v1/chat/completions",
            json={**body, "temperature": 0.5},
            headers=headers,
        )

    assert first.status_code == 200
    assert first.headers["Idempotent-Replayed"] == "false"
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json() == second.json()
    assert conflict.status_code == 409
    assert calls == 1

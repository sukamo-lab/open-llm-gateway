import asyncio
import time

from fastapi.testclient import TestClient

from llm_gateway.app import create_app


def test_batch_submission_execution_and_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_KEY", "a" * 32)
    config = tmp_path / "batch.yaml"
    config.write_text(
        f"mode: multi\ndatabase: {tmp_path / 'batch.db'}\n"
        "features:\n  async_jobs: true\n  batch_api: true\n"
        "batch:\n  allow_payload_persistence: true\n"
        "jobs:\n  poll_interval_seconds: 0.01\n"
        "providers:\n  - name: p\n    base_url: https://example.test/v1\n"
        "    api_key_value: key\n    models:\n"
        "      - {name: fast, upstream_model: model}\n",
        encoding="utf-8",
    )
    admin = {"Authorization": f"Bearer {'a' * 32}"}
    with TestClient(create_app(str(config))) as client:
        tenant = client.post("/admin/tenants", json={"name": "batch"}, headers=admin).json()
        issued = client.post(
            f"/admin/tenants/{tenant['id']}/keys", json={"name": "batch"}, headers=admin,
        ).json()
        consumer = {"Authorization": f"Bearer {issued['api_key']}"}

        async def complete(payload, *_args, **_kwargs):
            await asyncio.sleep(0)
            return {"id": payload["messages"][0]["content"]}, "p", "model"

        client.app.state.router.complete_for = complete
        submitted = client.post(
            "/v1/batches", headers=consumer, json={"requests": [
                {"custom_id": "one", "body": {
                    "model": "fast", "messages": [{"role": "user", "content": "1"}],
                }},
                {"custom_id": "two", "body": {
                    "model": "fast", "messages": [{"role": "user", "content": "2"}],
                }},
            ]},
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            status = client.get(f"/v1/batches/{job_id}", headers=consumer)
            if status.json()["state"] == "succeeded":
                break
            time.sleep(0.02)
        assert status.json()["state"] == "succeeded"
        assert [
            item["custom_id"] for item in status.json()["result"]["results"]
        ] == ["one", "two"]
        assert client.get(
            f"/v1/batches/{job_id}", headers={"Authorization": "Bearer invalid"},
        ).status_code == 401

import pytest
import sqlite3
import hashlib
import hmac
import json

import httpx

from llm_gateway.jobs import JobWorker, SignedWebhookNotifier, SQLiteJobStore, WebhookDeliveryWorker


@pytest.mark.asyncio
async def test_jobs_are_durable_tenant_isolated_and_cancellable(tmp_path):
    database = str(tmp_path / "jobs.db")
    store = SQLiteJobStore(database)
    await store.initialize()
    job = await store.create("tenant-a", "batch", {"input": "opaque-reference"})

    reopened = SQLiteJobStore(database)
    await reopened.initialize()
    assert (await reopened.get("tenant-a", job["id"]))["state"] == "queued"
    assert await reopened.get("tenant-b", job["id"]) is None

    cancelled = await reopened.cancel("tenant-a", job["id"])
    assert cancelled and cancelled["state"] == "cancelled"
    assert (await reopened.cancel("tenant-a", job["id"]))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_expired_job_lease_is_recovered_and_stale_worker_cannot_complete(tmp_path):
    database = str(tmp_path / "recovery.db")
    store = SQLiteJobStore(database)
    await store.initialize()
    job = await store.create("tenant", "batch", {"items": [1]})
    first = await store.claim("worker-a", 60)
    assert first and first["id"] == job["id"] and first["attempts"] == 1
    with sqlite3.connect(database) as db:
        db.execute("UPDATE async_jobs SET lease_until=0 WHERE id=?", (job["id"],))
    recovered = await store.claim("worker-b", 60)
    assert recovered and recovered["attempts"] == 2
    assert not await store.complete(job["id"], "worker-a", {"wrong": True})
    assert await store.complete(job["id"], "worker-b", {"ok": True})
    assert (await store.get("tenant", job["id"]))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_worker_records_safe_failure_codes_and_results(tmp_path):
    store = SQLiteJobStore(str(tmp_path / "worker.db"))
    await store.initialize()
    success = await store.create("tenant", "ok", {"value": 2})
    failure = await store.create("tenant", "missing", {})

    async def double(job):
        return {"value": job["payload"]["value"] * 2}

    worker = JobWorker(store, {"ok": double}, worker_id="worker")
    assert await worker.run_once()
    assert await worker.run_once()
    assert (await store.get("tenant", success["id"]))["state"] == "succeeded"
    failed = await store.get("tenant", failure["id"])
    assert failed["state"] == "failed" and failed["error_code"] == "unknown_job_kind"


@pytest.mark.asyncio
async def test_signed_webhook_retries_with_stable_idempotency_key(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500 if len(requests) == 1 else 204)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("llm_gateway.jobs.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = SignedWebhookNotifier("https://sink.test/jobs", "secret", client, attempts=2)
        await notifier({"id": "job-1", "tenant_id": "tenant", "kind": "batch", "state": "succeeded"})

    assert len(requests) == 2
    assert requests[0].headers["Idempotency-Key"] == "job-1"
    timestamp, signature = requests[0].headers["X-Gateway-Signature"].split(",")
    expected = hmac.new(
        b"secret", timestamp[2:].encode() + b"." + requests[0].content, hashlib.sha256,
    ).hexdigest()
    assert signature == f"v1={expected}"
    assert json.loads(requests[0].content)["state"] == "succeeded"


@pytest.mark.asyncio
async def test_webhook_outbox_survives_restart_and_retries(tmp_path):
    database = str(tmp_path / "webhook-outbox.db")
    store = SQLiteJobStore(database)
    await store.initialize()
    job = await store.create("tenant", "ok", {})

    async def handler(_job):
        return {"ok": True}

    async def configured_notifier(_event):
        return None

    worker = JobWorker(store, {"ok": handler}, notifier=configured_notifier)
    assert await worker.run_once()

    reopened = SQLiteJobStore(database)
    await reopened.initialize()
    attempts = 0

    async def flaky_notifier(event):
        nonlocal attempts
        attempts += 1
        assert event["id"] == job["id"]
        if attempts == 1:
            raise RuntimeError("temporary failure")

    delivery = WebhookDeliveryWorker(reopened, flaky_notifier, max_retry_seconds=0)
    assert await delivery.run_once()
    assert await delivery.run_once()
    assert attempts == 2
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT state,attempts,last_error FROM job_webhook_deliveries WHERE job_id=?",
            (job["id"],),
        ).fetchone()
    assert row == ("delivered", 2, None)

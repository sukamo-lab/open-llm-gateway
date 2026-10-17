import sqlite3

import pytest

from llm_gateway.audit import GENESIS_HASH, SQLiteAuditStore, safe_metadata
from scripts.verify_audit_export import load_events


@pytest.mark.asyncio
async def test_audit_chain_is_append_only_and_detects_corruption(tmp_path):
    database = str(tmp_path / "audit.db")
    store = SQLiteAuditStore(database)
    await store.initialize()
    first = await store.append("tenant", "actor", "organization.created", "success", "org")
    second = await store.append("tenant", "actor", "project.created", "success", "org", {"project_id": "p"})
    assert second["previous_hash"] == first["event_hash"]
    assert (await store.verify())[0] is True

    with sqlite3.connect(database) as db, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_events SET outcome='changed' WHERE sequence=1")

    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER audit_events_no_update")
        db.execute("UPDATE audit_events SET outcome='changed' WHERE sequence=1")
    valid, sequence, _ = await store.verify()
    assert valid is False and sequence == 2


@pytest.mark.asyncio
async def test_audit_export_replays_after_failure_without_advancing_checkpoint(tmp_path):
    store = SQLiteAuditStore(str(tmp_path / "audit.db"))
    await store.initialize()
    await store.append("tenant", "actor", "one", "success")
    await store.append("tenant", "actor", "two", "success")
    attempts = []

    async def failing(events):
        attempts.append(events)
        raise RuntimeError("sink unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        await store.export("sink", 1, failing)
    assert await store.checkpoint("sink") == (0, GENESIS_HASH)

    exported = []

    async def success(events):
        exported.extend(events)

    assert await store.export("sink", 1, success) == 1
    assert exported[0]["sequence"] == 1
    assert (await store.checkpoint("sink"))[0] == 1
    assert await store.export("sink", 10, success) == 1
    assert [event["sequence"] for event in exported] == [1, 2]


def test_audit_metadata_rejects_sensitive_or_nested_content():
    with pytest.raises(ValueError, match="sensitive"):
        safe_metadata({"api_key": "secret"})
    with pytest.raises(ValueError, match="scalar"):
        safe_metadata({"claims": {"sub": "customer"}})


def test_independent_export_loader_supports_batch_and_json_lines(tmp_path):
    batch = tmp_path / "batch.json"
    batch.write_text('{"events":[{"sequence":1}]}', encoding="utf-8")
    assert load_events(batch) == [{"sequence": 1}]
    lines = tmp_path / "events.jsonl"
    lines.write_text('{"sequence":1}\n{"sequence":2}\n', encoding="utf-8")
    assert [item["sequence"] for item in load_events(lines)] == [1, 2]

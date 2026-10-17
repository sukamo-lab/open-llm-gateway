from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from typing import Any, Awaitable, Callable

from .migrations import migrate_postgres, migrate_sqlite


GENESIS_HASH = "0" * 64
SENSITIVE_NAMES = frozenset({"token", "authorization", "api_key", "secret", "password", "prompt", "response", "content"})


def _bounded_identifier(name: str, value: str, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or not value.isprintable():
        raise ValueError(f"audit {name} is invalid")
    return value


def safe_metadata(metadata: dict[str, Any] | None) -> dict[str, str | int | float | bool | None]:
    result: dict[str, str | int | float | bool | None] = {}
    for key, value in (metadata or {}).items():
        normalized = key.lower().replace("-", "_")
        if any(name in normalized for name in SENSITIVE_NAMES):
            raise ValueError(f"sensitive audit metadata field is forbidden: {key}")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("audit metadata values must be scalar")
        if isinstance(value, str) and len(value) > 512:
            raise ValueError("audit metadata value is too long")
        result[key] = value
    return result


def event_hash(event: dict[str, Any]) -> str:
    material = {key: event[key] for key in (
        "sequence", "tenant_id", "organization_id", "actor_id", "event_type",
        "outcome", "metadata", "created_at", "previous_hash",
    )}
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_event(row: Any) -> dict[str, Any]:
    event = dict(row)
    event["metadata"] = json.loads(event.pop("metadata_json"))
    return event


def verify_events(
    events: list[dict[str, Any]], checkpoint_hash: str = GENESIS_HASH,
    checkpoint_sequence: int = 0,
) -> tuple[bool, str]:
    previous = checkpoint_hash
    last_sequence = checkpoint_sequence
    for event in events:
        if event["sequence"] != last_sequence + 1 or event["previous_hash"] != previous:
            return False, previous
        if event_hash(event) != event["event_hash"]:
            return False, previous
        previous, last_sequence = event["event_hash"], event["sequence"]
    return True, previous


class SQLiteAuditStore:
    def __init__(self, database: str):
        self.database = database
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(migrate_sqlite, self.database, ("audit",))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    async def append(
        self, tenant_id: str, actor_id: str, event_type: str, outcome: str,
        organization_id: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        bounded = safe_metadata(metadata)
        tenant_id = _bounded_identifier("tenant_id", tenant_id)
        actor_id = _bounded_identifier("actor_id", actor_id)
        event_type = _bounded_identifier("event_type", event_type, 128)
        outcome = _bounded_identifier("outcome", outcome, 64)
        if organization_id is not None:
            organization_id = _bounded_identifier("organization_id", organization_id)

        def write() -> dict[str, Any]:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                last = db.execute(
                    "SELECT sequence,event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence = int(last["sequence"]) + 1 if last else 1
                event = {
                    "sequence": sequence, "tenant_id": tenant_id,
                    "organization_id": organization_id, "actor_id": actor_id,
                    "event_type": event_type, "outcome": outcome, "metadata": bounded,
                    "created_at": time.time(), "previous_hash": last["event_hash"] if last else GENESIS_HASH,
                }
                event["event_hash"] = event_hash(event)
                db.execute(
                    """INSERT INTO audit_events
                    (sequence,tenant_id,organization_id,actor_id,event_type,outcome,metadata_json,
                    created_at,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (sequence, tenant_id, organization_id, actor_id, event_type, outcome,
                     json.dumps(bounded, sort_keys=True, separators=(",", ":")), event["created_at"],
                     event["previous_hash"], event["event_hash"]),
                )
                return event

        async with self._lock:
            return await asyncio.to_thread(write)

    async def read_after(self, sequence: int, limit: int) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence LIMIT ?",
                    (sequence, limit),
                ).fetchall()
            return [_row_event(row) for row in rows]
        return await asyncio.to_thread(read)

    async def verify(self) -> tuple[bool, int, str]:
        events = await self.read_after(0, 2_147_483_647)
        valid, last_hash = verify_events(events)
        return valid, events[-1]["sequence"] if events else 0, last_hash

    async def checkpoint(self, sink_id: str) -> tuple[int, str]:
        def read() -> tuple[int, str]:
            with self._connect() as db:
                row = db.execute(
                    "SELECT sequence,event_hash FROM audit_export_checkpoints WHERE sink_id=?", (sink_id,)
                ).fetchone()
            return (int(row["sequence"]), row["event_hash"]) if row else (0, GENESIS_HASH)
        return await asyncio.to_thread(read)

    async def export(
        self, sink_id: str, batch_size: int,
        sink: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> int:
        sequence, previous_hash = await self.checkpoint(sink_id)
        events = await self.read_after(sequence, batch_size)
        if not events:
            return 0
        valid, _ = verify_events(events, previous_hash, sequence)
        if not valid:
            raise RuntimeError("audit export chain verification failed")
        await sink(events)
        last = events[-1]

        def save() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO audit_export_checkpoints(sink_id,sequence,event_hash,updated_at)
                    VALUES(?,?,?,?) ON CONFLICT(sink_id) DO UPDATE SET
                    sequence=excluded.sequence,event_hash=excluded.event_hash,updated_at=excluded.updated_at""",
                    (sink_id, last["sequence"], last["event_hash"], time.time()),
                )
        await asyncio.to_thread(save)
        return len(events)


class PostgresAuditStore:
    def __init__(self, dsn: str):
        self.dsn, self.pool = dsn, None

    async def initialize(self) -> None:
        import asyncpg
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10, command_timeout=30)
        await migrate_postgres(self.pool, ("audit",))

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def append(self, tenant_id: str, actor_id: str, event_type: str, outcome: str,
                     organization_id: str | None = None, metadata: dict[str, Any] | None = None):
        assert self.pool
        bounded = safe_metadata(metadata)
        tenant_id = _bounded_identifier("tenant_id", tenant_id)
        actor_id = _bounded_identifier("actor_id", actor_id)
        event_type = _bounded_identifier("event_type", event_type, 128)
        outcome = _bounded_identifier("outcome", outcome, 64)
        if organization_id is not None:
            organization_id = _bounded_identifier("organization_id", organization_id)
        async with self.pool.acquire() as db, db.transaction():
            await db.execute("SELECT pg_advisory_xact_lock(hashtext('llm-gateway-audit-chain'))")
            last = await db.fetchrow("SELECT sequence,event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1")
            sequence = int(last["sequence"]) + 1 if last else 1
            event = {"sequence": sequence, "tenant_id": tenant_id, "organization_id": organization_id,
                     "actor_id": actor_id, "event_type": event_type, "outcome": outcome,
                     "metadata": bounded, "created_at": time.time(),
                     "previous_hash": last["event_hash"] if last else GENESIS_HASH}
            event["event_hash"] = event_hash(event)
            await db.execute("""INSERT INTO audit_events(sequence,tenant_id,organization_id,actor_id,
                event_type,outcome,metadata_json,created_at,previous_hash,event_hash)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""", sequence, tenant_id,
                organization_id, actor_id, event_type, outcome,
                json.dumps(bounded, sort_keys=True, separators=(",", ":")), event["created_at"],
                event["previous_hash"], event["event_hash"])
            return event

    async def read_after(self, sequence: int, limit: int):
        assert self.pool
        return [_row_event(row) for row in await self.pool.fetch(
            "SELECT * FROM audit_events WHERE sequence>$1 ORDER BY sequence LIMIT $2", sequence, limit
        )]

    async def verify(self):
        events = await self.read_after(0, 2_147_483_647)
        valid, last_hash = verify_events(events)
        return valid, events[-1]["sequence"] if events else 0, last_hash

    async def checkpoint(self, sink_id: str):
        assert self.pool
        row = await self.pool.fetchrow(
            "SELECT sequence,event_hash FROM audit_export_checkpoints WHERE sink_id=$1", sink_id
        )
        return (int(row["sequence"]), row["event_hash"]) if row else (0, GENESIS_HASH)

    async def export(self, sink_id: str, batch_size: int, sink):
        assert self.pool
        sequence, previous_hash = await self.checkpoint(sink_id)
        events = await self.read_after(sequence, batch_size)
        if not events:
            return 0
        valid, _ = verify_events(events, previous_hash, sequence)
        if not valid:
            raise RuntimeError("audit export chain verification failed")
        await sink(events)
        last = events[-1]
        await self.pool.execute("""INSERT INTO audit_export_checkpoints(sink_id,sequence,event_hash,updated_at)
            VALUES($1,$2,$3,$4) ON CONFLICT(sink_id) DO UPDATE SET
            sequence=EXCLUDED.sequence,event_hash=EXCLUDED.event_hash,updated_at=EXCLUDED.updated_at""",
            sink_id, last["sequence"], last["event_hash"], time.time())
        return len(events)

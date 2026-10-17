from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .migrations import migrate_postgres, migrate_sqlite


@dataclass(frozen=True)
class IdempotencyClaim:
    status: Literal["owner", "cached", "pending", "conflict"]
    owner_token: str | None = None
    response: dict[str, Any] | None = None


def key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SQLiteIdempotencyStore:
    def __init__(
        self, database: str, ttl_seconds: int, lease_seconds: int,
        cipher=None, allow_plaintext_migration: bool = False,
    ):
        self.database = database
        self.ttl_seconds = ttl_seconds
        self.lease_seconds = lease_seconds
        self.cipher = cipher
        self.allow_plaintext_migration = allow_plaintext_migration
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    async def initialize(self) -> None:
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            await asyncio.to_thread(migrate_sqlite, self.database, ("idempotency",))
            await asyncio.to_thread(self._cleanup)

    def _cleanup(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM idempotency_records WHERE expires_at <= ?", (time.time(),))

    async def claim(self, tenant_id: str, digest: str, fingerprint: str) -> IdempotencyClaim:
        async with self._lock:
            return await asyncio.to_thread(self._claim, tenant_id, digest, fingerprint)

    def _claim(self, tenant_id: str, digest: str, fingerprint: str) -> IdempotencyClaim:
        now, owner = time.time(), secrets.token_urlsafe(24)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT request_hash,owner_token,state,response_json,expires_at,lease_until
                FROM idempotency_records WHERE tenant_id=? AND key_hash=?""",
                (tenant_id, digest),
            ).fetchone()
            if row and row["expires_at"] <= now:
                db.execute(
                    "DELETE FROM idempotency_records WHERE tenant_id=? AND key_hash=?",
                    (tenant_id, digest),
                )
                row = None
            if not row:
                db.execute(
                    """INSERT INTO idempotency_records
                    (tenant_id,key_hash,request_hash,owner_token,state,response_json,
                     created_at,expires_at,lease_until)
                    VALUES(?,?,?,?, 'pending',NULL,?,?,?)""",
                    (tenant_id, digest, fingerprint, owner, now,
                     now + self.ttl_seconds, now + self.lease_seconds),
                )
                return IdempotencyClaim("owner", owner_token=owner)
            if row["request_hash"] != fingerprint:
                return IdempotencyClaim("conflict")
            if row["state"] == "completed" and row["response_json"]:
                value = row["response_json"]
                if self.cipher:
                    if value.startswith("enc:v1:"):
                        value = self.cipher.decrypt(value, f"idempotency:{tenant_id}:{digest}")
                    elif not self.allow_plaintext_migration:
                        raise RuntimeError("unencrypted idempotency response requires migration")
                try:
                    return IdempotencyClaim("cached", response=json.loads(value))
                except (TypeError, ValueError):
                    db.execute(
                        "DELETE FROM idempotency_records WHERE tenant_id=? AND key_hash=?",
                        (tenant_id, digest),
                    )
                    return IdempotencyClaim("pending")
            if row["lease_until"] <= now:
                db.execute(
                    """UPDATE idempotency_records SET owner_token=?,lease_until=?
                    WHERE tenant_id=? AND key_hash=?""",
                    (owner, now + self.lease_seconds, tenant_id, digest),
                )
                return IdempotencyClaim("owner", owner_token=owner)
            return IdempotencyClaim("pending")

    async def complete(
        self, tenant_id: str, digest: str, owner_token: str, response_json: str
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._complete, tenant_id, digest, owner_token, response_json
            )

    def _complete(self, tenant_id: str, digest: str, owner_token: str, response_json: str) -> bool:
        if self.cipher:
            response_json = self.cipher.encrypt(response_json, f"idempotency:{tenant_id}:{digest}")
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE idempotency_records SET state='completed',response_json=?,lease_until=0
                WHERE tenant_id=? AND key_hash=? AND owner_token=? AND state='pending'""",
                (response_json, tenant_id, digest, owner_token),
            )
        return cursor.rowcount == 1

    async def release(self, tenant_id: str, digest: str, owner_token: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._release, tenant_id, digest, owner_token)

    def _release(self, tenant_id: str, digest: str, owner_token: str) -> None:
        with self._connect() as db:
            db.execute(
                """DELETE FROM idempotency_records
                WHERE tenant_id=? AND key_hash=? AND owner_token=? AND state='pending'""",
                (tenant_id, digest, owner_token),
            )

    async def close(self) -> None:
        return None


class PostgresIdempotencyStore:
    def __init__(
        self, dsn: str, ttl_seconds: int, lease_seconds: int,
        cipher=None, allow_plaintext_migration: bool = False,
    ):
        self.dsn = dsn
        self.ttl_seconds = ttl_seconds
        self.lease_seconds = lease_seconds
        self.cipher = cipher
        self.allow_plaintext_migration = allow_plaintext_migration
        self.pool = None

    async def initialize(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10, command_timeout=30)
        await migrate_postgres(self.pool, ("idempotency",))
        await self.pool.execute("DELETE FROM idempotency_records WHERE expires_at <= $1", time.time())

    async def claim(self, tenant_id: str, digest: str, fingerprint: str) -> IdempotencyClaim:
        assert self.pool
        now, owner = time.time(), secrets.token_urlsafe(24)
        async with self.pool.acquire() as db, db.transaction():
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"idempotency:{tenant_id}:{digest}",
            )
            row = await db.fetchrow(
                """SELECT request_hash,owner_token,state,response_json,expires_at,lease_until
                FROM idempotency_records WHERE tenant_id=$1 AND key_hash=$2 FOR UPDATE""",
                tenant_id, digest,
            )
            if row and row["expires_at"] <= now:
                await db.execute(
                    "DELETE FROM idempotency_records WHERE tenant_id=$1 AND key_hash=$2",
                    tenant_id, digest,
                )
                row = None
            if not row:
                await db.execute(
                    """INSERT INTO idempotency_records
                    (tenant_id,key_hash,request_hash,owner_token,state,response_json,
                     created_at,expires_at,lease_until)
                    VALUES($1,$2,$3,$4,'pending',NULL,$5,$6,$7)""",
                    tenant_id, digest, fingerprint, owner, now,
                    now + self.ttl_seconds, now + self.lease_seconds,
                )
                return IdempotencyClaim("owner", owner_token=owner)
            if row["request_hash"] != fingerprint:
                return IdempotencyClaim("conflict")
            if row["state"] == "completed" and row["response_json"]:
                value = row["response_json"]
                if self.cipher:
                    if value.startswith("enc:v1:"):
                        value = self.cipher.decrypt(value, f"idempotency:{tenant_id}:{digest}")
                    elif not self.allow_plaintext_migration:
                        raise RuntimeError("unencrypted idempotency response requires migration")
                try:
                    return IdempotencyClaim("cached", response=json.loads(value))
                except (TypeError, ValueError):
                    await db.execute(
                        "DELETE FROM idempotency_records WHERE tenant_id=$1 AND key_hash=$2",
                        tenant_id, digest,
                    )
                    return IdempotencyClaim("pending")
            if row["lease_until"] <= now:
                await db.execute(
                    """UPDATE idempotency_records SET owner_token=$1,lease_until=$2
                    WHERE tenant_id=$3 AND key_hash=$4""",
                    owner, now + self.lease_seconds, tenant_id, digest,
                )
                return IdempotencyClaim("owner", owner_token=owner)
            return IdempotencyClaim("pending")

    async def complete(
        self, tenant_id: str, digest: str, owner_token: str, response_json: str
    ) -> bool:
        assert self.pool
        if self.cipher:
            response_json = self.cipher.encrypt(response_json, f"idempotency:{tenant_id}:{digest}")
        result = await self.pool.execute(
            """UPDATE idempotency_records SET state='completed',response_json=$1,lease_until=0
            WHERE tenant_id=$2 AND key_hash=$3 AND owner_token=$4 AND state='pending'""",
            response_json, tenant_id, digest, owner_token,
        )
        return result.endswith("1")

    async def release(self, tenant_id: str, digest: str, owner_token: str) -> None:
        assert self.pool
        await self.pool.execute(
            """DELETE FROM idempotency_records
            WHERE tenant_id=$1 AND key_hash=$2 AND owner_token=$3 AND state='pending'""",
            tenant_id, digest, owner_token,
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

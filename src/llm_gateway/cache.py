from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .migrations import migrate_postgres, migrate_sqlite


@dataclass(frozen=True)
class CacheEntry:
    response: dict[str, Any]
    created_at: float


def exact_cache_key(payload: dict[str, Any], namespace: str, routing_signature: str) -> str:
    canonical = json.dumps(
        {"namespace": namespace, "routing": routing_signature, "request": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MemoryCacheStore:
    def __init__(self, ttl_seconds: int, max_entries: int):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], tuple[float, CacheEntry]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def get(self, tenant_id: str, key: str) -> CacheEntry | None:
        now = time.time()
        identity = (tenant_id, key)
        async with self._lock:
            value = self._entries.get(identity)
            if not value:
                return None
            expires_at, entry = value
            if expires_at <= now:
                self._entries.pop(identity, None)
                return None
            self._entries.move_to_end(identity)
            return entry

    async def put(self, tenant_id: str, key: str, response_json: str) -> None:
        now = time.time()
        entry = CacheEntry(json.loads(response_json), now)
        identity = (tenant_id, key)
        async with self._lock:
            self._entries[identity] = (now + self.ttl_seconds, entry)
            self._entries.move_to_end(identity)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    async def close(self) -> None:
        self._entries.clear()


class SQLiteCacheStore:
    def __init__(self, database: str, ttl_seconds: int, max_entries: int):
        self.database = database
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    async def initialize(self) -> None:
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            await asyncio.to_thread(migrate_sqlite, self.database, ("cache",))
            await asyncio.to_thread(self._cleanup)

    def _cleanup(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM exact_cache_entries WHERE expires_at <= ?", (time.time(),))

    async def get(self, tenant_id: str, key: str) -> CacheEntry | None:
        async with self._lock:
            return await asyncio.to_thread(self._get, tenant_id, key)

    def _get(self, tenant_id: str, key: str) -> CacheEntry | None:
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                """SELECT response_json,created_at FROM exact_cache_entries
                WHERE tenant_id=? AND cache_key=? AND expires_at>?""",
                (tenant_id, key, now),
            ).fetchone()
        if not row:
            return None
        try:
            return CacheEntry(json.loads(row["response_json"]), float(row["created_at"]))
        except (TypeError, ValueError):
            return None

    async def put(self, tenant_id: str, key: str, response_json: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._put, tenant_id, key, response_json)

    def _put(self, tenant_id: str, key: str, response_json: str) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """INSERT INTO exact_cache_entries
                (tenant_id,cache_key,response_json,created_at,expires_at) VALUES(?,?,?,?,?)
                ON CONFLICT(tenant_id,cache_key) DO UPDATE SET
                response_json=excluded.response_json,created_at=excluded.created_at,
                expires_at=excluded.expires_at""",
                (tenant_id, key, response_json, now, now + self.ttl_seconds),
            )
            db.execute("DELETE FROM exact_cache_entries WHERE expires_at <= ?", (now,))
            excess = db.execute(
                "SELECT MAX(0,COUNT(*)-?) FROM exact_cache_entries", (self.max_entries,)
            ).fetchone()[0]
            if excess:
                db.execute(
                    """DELETE FROM exact_cache_entries WHERE rowid IN
                    (SELECT rowid FROM exact_cache_entries ORDER BY created_at LIMIT ?)""",
                    (excess,),
                )

    async def close(self) -> None:
        return None


class PostgresCacheStore:
    def __init__(self, dsn: str, ttl_seconds: int, max_entries: int):
        self.dsn = dsn
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.pool = None

    async def initialize(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10, command_timeout=30)
        await migrate_postgres(self.pool, ("cache",))
        await self.pool.execute("DELETE FROM exact_cache_entries WHERE expires_at <= $1", time.time())

    async def get(self, tenant_id: str, key: str) -> CacheEntry | None:
        assert self.pool
        row = await self.pool.fetchrow(
            """SELECT response_json,created_at FROM exact_cache_entries
            WHERE tenant_id=$1 AND cache_key=$2 AND expires_at>$3""",
            tenant_id, key, time.time(),
        )
        if not row:
            return None
        try:
            return CacheEntry(json.loads(row["response_json"]), float(row["created_at"]))
        except (TypeError, ValueError):
            return None

    async def put(self, tenant_id: str, key: str, response_json: str) -> None:
        assert self.pool
        now = time.time()
        async with self.pool.acquire() as db, db.transaction():
            await db.execute(
                """INSERT INTO exact_cache_entries
                (tenant_id,cache_key,response_json,created_at,expires_at) VALUES($1,$2,$3,$4,$5)
                ON CONFLICT(tenant_id,cache_key) DO UPDATE SET
                response_json=EXCLUDED.response_json,created_at=EXCLUDED.created_at,
                expires_at=EXCLUDED.expires_at""",
                tenant_id, key, response_json, now, now + self.ttl_seconds,
            )
            await db.execute("DELETE FROM exact_cache_entries WHERE expires_at <= $1", now)
            total = await db.fetchval("SELECT COUNT(*) FROM exact_cache_entries")
            excess = max(0, int(total) - self.max_entries)
            if excess:
                await db.execute(
                    """DELETE FROM exact_cache_entries WHERE (tenant_id,cache_key) IN
                    (SELECT tenant_id,cache_key FROM exact_cache_entries
                     ORDER BY created_at LIMIT $1)""",
                    excess,
                )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None


class RedisCacheStore:
    def __init__(self, url: str, ttl_seconds: int, prefix: str):
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.prefix = prefix
        self.client = None

    async def initialize(self) -> None:
        from redis.asyncio import from_url

        self.client = from_url(self.url, decode_responses=True)
        await self.client.ping()

    def _key(self, tenant_id: str, key: str) -> str:
        identity = hashlib.sha256(f"{tenant_id}:{key}".encode("utf-8")).hexdigest()
        return f"{self.prefix}:{identity}"

    async def get(self, tenant_id: str, key: str) -> CacheEntry | None:
        value = await self.client.get(self._key(tenant_id, key))
        if not value:
            return None
        try:
            envelope = json.loads(value)
            return CacheEntry(envelope["response"], float(envelope["created_at"]))
        except (KeyError, TypeError, ValueError):
            return None

    async def put(self, tenant_id: str, key: str, response_json: str) -> None:
        now = time.time()
        envelope = json.dumps(
            {"created_at": now, "response": json.loads(response_json)},
            ensure_ascii=False, separators=(",", ":"),
        )
        await self.client.set(self._key(tenant_id, key), envelope, ex=self.ttl_seconds)

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None

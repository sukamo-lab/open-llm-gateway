from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import secrets
import sqlite3
import time
import uuid
from collections import OrderedDict

from .config import Limit
from .migrations import migrate_sqlite


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    key_id: str
    limits: tuple[Limit, ...] = ()


class IdentityStore:
    """Tenant and consumer-key store. Plaintext keys are never persisted."""

    def __init__(self, database: str, cache_seconds: int = 60, cache_max_entries: int = 10_000):
        self.database = database
        self.cache_seconds = cache_seconds
        self.cache_max_entries = cache_max_entries
        self._write_lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float, AuthContext | None]] = OrderedDict()

    def _cache_put(self, digest: str, value: AuthContext | None, expires: float) -> None:
        if self.cache_max_entries <= 0:
            return
        self._cache[digest] = (expires, value)
        self._cache.move_to_end(digest)
        while len(self._cache) > self.cache_max_entries:
            self._cache.popitem(last=False)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    async def initialize(self) -> None:
        async with self._write_lock:
            migrate_sqlite(self.database, ("identity",))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _limits(row: sqlite3.Row) -> tuple[Limit, ...]:
        result = []
        if row["requests_per_minute"]:
            result.append(Limit(requests=row["requests_per_minute"], window_seconds=60))
        if row["tokens_per_day"]:
            result.append(Limit(tokens=row["tokens_per_day"], window_seconds=86400))
        if row["cost_per_30_days"]:
            result.append(Limit(cost_usd=row["cost_per_30_days"], window_seconds=2592000))
        return tuple(result)

    async def authenticate(self, token: str) -> AuthContext | None:
        digest = self._hash(token)
        cached = self._cache.get(digest)
        now = time.monotonic()
        if cached and cached[0] > now:
            self._cache.move_to_end(digest)
            return cached[1]

        def lookup() -> AuthContext | None:
            with self._connect() as db:
                row = db.execute(
                    """SELECT k.id key_id, t.* FROM consumer_keys k
                    JOIN tenants t ON t.id = k.tenant_id
                    WHERE k.key_hash = ? AND k.active = 1 AND t.active = 1
                    AND (k.expires_at IS NULL OR k.expires_at > ?)""",
                    (digest, time.time()),
                ).fetchone()
                if not row:
                    return None
                db.execute("UPDATE consumer_keys SET last_used_at = ? WHERE id = ?", (time.time(), row["key_id"]))
                return AuthContext(row["id"], row["key_id"], self._limits(row))

        context = await asyncio.to_thread(lookup)
        self._cache_put(digest, context, now + self.cache_seconds)
        return context

    async def create_tenant(
        self, name: str, requests_per_minute: int | None = None,
        tokens_per_day: int | None = None, cost_per_30_days: float | None = None,
    ) -> dict[str, object]:
        tenant_id = str(uuid.uuid4())
        async with self._write_lock:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO tenants VALUES (?, ?, 1, ?, ?, ?, ?)",
                    (tenant_id, name, requests_per_minute, tokens_per_day,
                     cost_per_30_days, time.time()),
                )
        return {"id": tenant_id, "name": name, "active": True}

    async def list_tenants(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, name, active, requests_per_minute, tokens_per_day,
                cost_per_30_days, created_at FROM tenants ORDER BY created_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    async def issue_key(
        self, tenant_id: str, name: str, expires_in_days: int | None = None
    ) -> dict[str, object]:
        key_id = uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        token = f"lgw_live_{key_id}.{secret}"
        async with self._write_lock:
            with self._connect() as db:
                exists = db.execute("SELECT 1 FROM tenants WHERE id = ? AND active = 1", (tenant_id,)).fetchone()
                if not exists:
                    raise KeyError("tenant not found or disabled")
                expires_at = time.time() + expires_in_days * 86400 if expires_in_days else None
                db.execute(
                    """INSERT INTO consumer_keys
                    (id,tenant_id,name,key_prefix,key_hash,active,created_at,last_used_at,expires_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, NULL, ?)""",
                    (key_id, tenant_id, name, token[:20], self._hash(token), time.time(), expires_at),
                )
        return {"id": key_id, "tenant_id": tenant_id, "name": name, "api_key": token, "expires_at": expires_at}

    async def list_keys(self, tenant_id: str) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, name, key_prefix, active, created_at, last_used_at, expires_at
                FROM consumer_keys WHERE tenant_id = ? ORDER BY created_at""", (tenant_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    async def revoke_key(self, key_id: str) -> bool:
        async with self._write_lock:
            with self._connect() as db:
                cursor = db.execute("UPDATE consumer_keys SET active = 0 WHERE id = ?", (key_id,))
        self._cache.clear()
        return cursor.rowcount > 0

    async def update_tenant(self, tenant_id: str, changes: dict[str, object]) -> dict[str, object] | None:
        allowed = {"name", "active", "requests_per_minute", "tokens_per_day", "cost_per_30_days"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if values:
            assignments = ", ".join(f"{key} = ?" for key in values)
            async with self._write_lock:
                with self._connect() as db:
                    db.execute(f"UPDATE tenants SET {assignments} WHERE id = ?", (*values.values(), tenant_id))
            self._cache.clear()
        with self._connect() as db:
            row = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections import OrderedDict

import asyncpg

from .config import Limit, ProviderConfig
from .identity import AuthContext
from .migrations import migrate_postgres, postgres_status
from .quota import Reservation


class PostgresStore:
    """Horizontally safe tenant and quota store using PostgreSQL transactions."""

    def __init__(
        self, dsn: str, cache_seconds: int = 5, cache_max_entries: int = 10_000,
        identity_enabled: bool = True, provider_quotas: bool = True,
        tenant_quotas: bool = True, accounting: bool = True,
        cost_tracking: bool = True,
    ):
        self.dsn = dsn
        self.cache_seconds = cache_seconds
        self.cache_max_entries = cache_max_entries
        self.identity_enabled = identity_enabled
        self.provider_quotas = provider_quotas
        self.tenant_quotas = tenant_quotas
        self.accounting = accounting
        self.cost_tracking = cost_tracking
        self.usage_enabled = provider_quotas or tenant_quotas or accounting
        self.pool: asyncpg.Pool | None = None
        self._cache: OrderedDict[str, tuple[float, AuthContext | None]] = OrderedDict()

    def _cache_put(self, digest: str, value: AuthContext | None, expires: float) -> None:
        if self.cache_max_entries <= 0:
            return
        self._cache[digest] = (expires, value)
        self._cache.move_to_end(digest)
        while len(self._cache) > self.cache_max_entries:
            self._cache.popitem(last=False)

    async def initialize(self) -> None:
        if self.pool:
            return
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=20, command_timeout=30)
        components = []
        if self.usage_enabled:
            components.append("usage")
        if self.identity_enabled:
            components.append("identity")
        await migrate_postgres(self.pool, components)

    async def migration_status(self) -> dict[str, int]:
        assert self.pool
        components = []
        if self.usage_enabled:
            components.append("usage")
        if self.identity_enabled:
            components.append("identity")
        return await postgres_status(self.pool, components)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    @staticmethod
    def _within(limit: Limit, requests: int, tokens: int, cost: float, estimate: int, estimate_cost: float) -> bool:
        return (
            (limit.requests is None or requests + 1 <= limit.requests)
            and (limit.tokens is None or tokens + estimate <= limit.tokens)
            and (limit.cost_usd is None or cost + estimate_cost <= limit.cost_usd)
        )

    async def reserve(
        self, provider: ProviderConfig, request_id: str, estimated_tokens: int,
        model: str | None = None, input_cost_per_million: float = 0,
        output_cost_per_million: float = 0, consumer_id: str | None = None,
        consumer_limits: tuple[Limit, ...] = (),
    ) -> Reservation | None:
        assert self.pool
        now = time.time()
        estimated_cost = (
            estimated_tokens * max(input_cost_per_million, output_cost_per_million) / 1_000_000
            if self.cost_tracking else 0
        )
        async with self.pool.acquire() as db, db.transaction():
            # Locks are shared by all gateway replicas and released with the transaction.
            await db.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"provider:{provider.name}")
            if consumer_id:
                await db.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"consumer:{consumer_id}")
            for limit, scope, value in [
                *((item, "provider", provider.name) for item in provider.limits if self.provider_quotas),
                *((item, "consumer_id", consumer_id) for item in consumer_limits if self.tenant_quotas),
            ]:
                column = "provider" if scope == "provider" else "consumer_id"
                row = await db.fetchrow(
                    f"""SELECT COALESCE(SUM(requests),0) requests,
                    COALESCE(SUM(tokens),0) tokens, COALESCE(SUM(cost_usd),0) cost
                    FROM usage_events WHERE {column}=$1 AND created_at >= $2
                    AND status != 'failed'""", value, now - limit.window_seconds,
                )
                if not self._within(limit, row["requests"], row["tokens"], row["cost"], estimated_tokens, estimated_cost):
                    return None
            await db.execute(
                """INSERT INTO usage_events
                (provider,request_id,created_at,requests,tokens,status,model,cost_usd,consumer_id)
                VALUES($1,$2,$3,1,$4,'reserved',$5,$6,$7)""",
                provider.name, request_id, now, estimated_tokens, model, estimated_cost, consumer_id,
            )
        return Reservation(
            provider.name, request_id, estimated_tokens, model,
            input_cost_per_million if self.cost_tracking else 0,
            output_cost_per_million if self.cost_tracking else 0,
        )

    async def reconcile(
        self, reservation: Reservation, actual_tokens: int | None, success: bool,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> None:
        assert self.pool
        if success:
            prompt, completion = int(prompt_tokens or 0), int(completion_tokens or 0)
            cost = (prompt * reservation.input_cost_per_million + completion * reservation.output_cost_per_million) / 1_000_000
            if prompt + completion == 0:
                cost = (actual_tokens or reservation.estimated_tokens) * max(
                    reservation.input_cost_per_million, reservation.output_cost_per_million
                ) / 1_000_000
            await self.pool.execute(
                """UPDATE usage_events SET tokens=$1,status='completed',prompt_tokens=$2,
                completion_tokens=$3,cost_usd=$4 WHERE request_id=$5""",
                actual_tokens or reservation.estimated_tokens, prompt, completion, cost, reservation.request_id,
            )
        else:
            await self.pool.execute(
                "UPDATE usage_events SET requests=0,tokens=0,cost_usd=0,status='failed' WHERE request_id=$1",
                reservation.request_id,
            )

    async def snapshot(self) -> list[dict[str, object]]:
        assert self.pool
        rows = await self.pool.fetch("""SELECT provider,SUM(requests) requests,SUM(tokens) tokens,
            MAX(created_at) last_used_at,SUM(prompt_tokens) prompt_tokens,
            SUM(completion_tokens) completion_tokens,SUM(cost_usd) cost_usd
            FROM usage_events WHERE status!='failed' GROUP BY provider""")
        return [dict(row) for row in rows]

    async def consumer_snapshot(self) -> list[dict[str, object]]:
        assert self.pool
        rows = await self.pool.fetch("""SELECT consumer_id tenant_id,SUM(requests) requests,
            SUM(tokens) tokens,SUM(cost_usd) cost_usd,MAX(created_at) last_used_at
            FROM usage_events WHERE status!='failed' AND consumer_id IS NOT NULL GROUP BY consumer_id""")
        return [dict(row) for row in rows]

    async def cleanup(self, before_timestamp: float) -> int:
        assert self.pool
        result = await self.pool.execute("DELETE FROM usage_events WHERE created_at < $1", before_timestamp)
        return int(result.rsplit(" ", 1)[-1])

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _limits(row) -> tuple[Limit, ...]:
        limits = []
        if row["requests_per_minute"]:
            limits.append(Limit(requests=row["requests_per_minute"], window_seconds=60))
        if row["tokens_per_day"]:
            limits.append(Limit(tokens=row["tokens_per_day"], window_seconds=86400))
        if row["cost_per_30_days"]:
            limits.append(Limit(cost_usd=row["cost_per_30_days"], window_seconds=2592000))
        return tuple(limits)

    async def authenticate(self, token: str) -> AuthContext | None:
        assert self.pool
        digest, now = self._hash(token), time.monotonic()
        cached = self._cache.get(digest)
        if cached and cached[0] > now:
            self._cache.move_to_end(digest)
            return cached[1]
        row = await self.pool.fetchrow("""SELECT k.id key_id,t.* FROM consumer_keys k
            JOIN tenants t ON t.id=k.tenant_id WHERE k.key_hash=$1 AND k.active AND t.active
            AND (k.expires_at IS NULL OR k.expires_at > $2)""", digest, time.time())
        context = AuthContext(row["id"], row["key_id"], self._limits(row)) if row else None
        if row:
            await self.pool.execute("UPDATE consumer_keys SET last_used_at=$1 WHERE id=$2", time.time(), row["key_id"])
        self._cache_put(digest, context, now + self.cache_seconds)
        return context

    async def create_tenant(self, name: str, requests_per_minute=None, tokens_per_day=None, cost_per_30_days=None):
        assert self.pool
        tenant_id = str(uuid.uuid4())
        await self.pool.execute("INSERT INTO tenants VALUES($1,$2,TRUE,$3,$4,$5,$6)", tenant_id, name, requests_per_minute, tokens_per_day, cost_per_30_days, time.time())
        return {"id": tenant_id, "name": name, "active": True}

    async def list_tenants(self):
        assert self.pool
        return [dict(row) for row in await self.pool.fetch("SELECT * FROM tenants ORDER BY created_at")]

    async def issue_key(self, tenant_id: str, name: str, expires_in_days: int | None = None):
        assert self.pool
        if not await self.pool.fetchval("SELECT 1 FROM tenants WHERE id=$1 AND active", tenant_id):
            raise KeyError("tenant not found or disabled")
        key_id, secret = uuid.uuid4().hex, secrets.token_urlsafe(32)
        token = f"lgw_live_{key_id}.{secret}"
        expires_at = time.time() + expires_in_days * 86400 if expires_in_days else None
        await self.pool.execute("""INSERT INTO consumer_keys
            (id,tenant_id,name,key_prefix,key_hash,active,created_at,last_used_at,expires_at)
            VALUES($1,$2,$3,$4,$5,TRUE,$6,NULL,$7)""",
            key_id, tenant_id, name, token[:20], self._hash(token), time.time(), expires_at)
        return {"id": key_id, "tenant_id": tenant_id, "name": name, "api_key": token, "expires_at": expires_at}

    async def list_keys(self, tenant_id: str):
        assert self.pool
        rows = await self.pool.fetch("SELECT id,name,key_prefix,active,created_at,last_used_at,expires_at FROM consumer_keys WHERE tenant_id=$1 ORDER BY created_at", tenant_id)
        return [dict(row) for row in rows]

    async def revoke_key(self, key_id: str) -> bool:
        assert self.pool
        result = await self.pool.execute("UPDATE consumer_keys SET active=FALSE WHERE id=$1", key_id)
        self._cache.clear()
        return result.endswith("1")

    async def update_tenant(self, tenant_id: str, changes: dict[str, object]):
        assert self.pool
        allowed = {"name", "active", "requests_per_minute", "tokens_per_day", "cost_per_30_days"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if values:
            assignments = ", ".join(f"{key}=${index}" for index, key in enumerate(values, 1))
            await self.pool.execute(
                f"UPDATE tenants SET {assignments} WHERE id=${len(values) + 1}",
                *values.values(), tenant_id,
            )
            self._cache.clear()
        row = await self.pool.fetchrow("SELECT * FROM tenants WHERE id=$1", tenant_id)
        return dict(row) if row else None

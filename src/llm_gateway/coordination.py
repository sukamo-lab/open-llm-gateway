from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
import time
import uuid

from .migrations import migrate_postgres


class PostgresCoordinator:
    def __init__(self, dsn: str, lease_seconds: float = 300):
        self.dsn, self.lease_seconds = dsn, lease_seconds
        self.pool = None
        self._open_until: dict[str, float] = {}

    async def initialize(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=20)
        await migrate_postgres(self.pool, ("coordination",))

    async def refresh(self) -> None:
        rows = await self.pool.fetch("SELECT provider,open_until FROM provider_circuits")
        self._open_until = {str(row["provider"]): float(row["open_until"]) for row in rows}

    def available(self, provider: str) -> bool:
        return self._open_until.get(provider, 0) <= time.time()

    async def succeeded(self, provider: str) -> None:
        await self.pool.execute(
            """INSERT INTO provider_circuits(provider,failures,open_until,updated_at)
            VALUES($1,0,0,$2) ON CONFLICT(provider) DO UPDATE SET
            failures=0,open_until=0,updated_at=EXCLUDED.updated_at""", provider, time.time(),
        )
        self._open_until.pop(provider, None)

    async def failed(self, provider: str, threshold: int, cooldown_seconds: int) -> None:
        now = time.time()
        row = await self.pool.fetchrow(
            """INSERT INTO provider_circuits(provider,failures,open_until,updated_at)
            VALUES($1,1,0,$2) ON CONFLICT(provider) DO UPDATE SET
            failures=provider_circuits.failures+1,updated_at=EXCLUDED.updated_at
            RETURNING failures""", provider, now,
        )
        if int(row["failures"]) >= threshold:
            opened = now + cooldown_seconds
            await self.pool.execute(
                "UPDATE provider_circuits SET open_until=$1 WHERE provider=$2", opened, provider,
            )
            self._open_until[provider] = opened

    async def acquire(self, provider: str, limit: int) -> str | None:
        token, now = str(uuid.uuid4()), time.time()
        async with self.pool.acquire() as connection, connection.transaction():
            # Serialize the count-and-insert decision for this provider across
            # every gateway replica. Row locks cannot protect an empty set.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"provider-slot:{provider}",
            )
            await connection.execute(
                "DELETE FROM provider_concurrency_leases WHERE expires_at<$1", now,
            )
            count = await connection.fetchval(
                "SELECT COUNT(*) FROM provider_concurrency_leases WHERE provider=$1", provider,
            )
            if int(count) >= limit:
                return None
            await connection.execute(
                """INSERT INTO provider_concurrency_leases(token,provider,expires_at)
                VALUES($1,$2,$3)""", token, provider, now + self.lease_seconds,
            )
        return token

    async def release(self, token: str) -> None:
        await self.pool.execute("DELETE FROM provider_concurrency_leases WHERE token=$1", token)

    async def renew(self, token: str) -> bool:
        result = await self.pool.execute(
            "UPDATE provider_concurrency_leases SET expires_at=$1 WHERE token=$2",
            time.time() + self.lease_seconds, token,
        )
        return result.endswith("1")

    async def _renew_until_cancelled(self, token: str) -> None:
        interval = max(self.lease_seconds / 3, 0.01)
        while True:
            await asyncio.sleep(interval)
            if not await self.renew(token):
                return

    @asynccontextmanager
    async def slot(self, provider: str, limit: int):
        token = None
        while token is None:
            token = await self.acquire(provider, limit)
            if token is None:
                await asyncio.sleep(0.05)
        renewer = asyncio.create_task(self._renew_until_cancelled(token))
        try:
            yield
        finally:
            renewer.cancel()
            with suppress(asyncio.CancelledError):
                await renewer
            await self.release(token)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

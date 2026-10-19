from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass

from .config import Limit, ProviderConfig
from .migrations import migrate_sqlite


@dataclass(frozen=True)
class Reservation:
    provider: str
    request_id: str
    estimated_tokens: int
    model: str | None = None
    input_cost_per_million: float = 0
    output_cost_per_million: float = 0
    persisted: bool = True


class QuotaStore:
    """Persistent rolling-window accounting with atomic reservations."""

    def __init__(
        self, database: str, provider_quotas: bool = True,
        tenant_quotas: bool = True, accounting: bool = True,
        cost_tracking: bool = True,
    ):
        self.database = database
        self.provider_quotas = provider_quotas
        self.tenant_quotas = tenant_quotas
        self.accounting = accounting
        self.cost_tracking = cost_tracking
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            migrate_sqlite(self.database, ("usage",))
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _within(
        limit: Limit, requests: int, tokens: int, cost_usd: float,
        estimate: int, estimated_cost_usd: float,
    ) -> bool:
        request_ok = limit.requests is None or requests + 1 <= limit.requests
        token_ok = limit.tokens is None or tokens + estimate <= limit.tokens
        cost_ok = limit.cost_usd is None or cost_usd + estimated_cost_usd <= limit.cost_usd
        return request_ok and token_ok and cost_ok

    async def reserve(
        self, provider: ProviderConfig, request_id: str, estimated_tokens: int,
        model: str | None = None, input_cost_per_million: float = 0,
        output_cost_per_million: float = 0,
        consumer_id: str | None = None, consumer_limits: tuple[Limit, ...] = (),
    ) -> Reservation | None:
        now = time.time()
        estimated_cost = estimated_tokens * max(
            input_cost_per_million, output_cost_per_million
        ) / 1_000_000 if self.cost_tracking else 0
        async with self._lock:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for limit in provider.limits if self.provider_quotas else ():
                    row = db.execute(
                        """SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(tokens), 0),
                        COALESCE(SUM(cost_usd), 0)
                        FROM usage_events WHERE provider = ? AND created_at >= ?
                        AND status != 'failed'""",
                        (provider.name, now - limit.window_seconds),
                    ).fetchone()
                    if not self._within(
                        limit, int(row[0]), int(row[1]), float(row[2]),
                        estimated_tokens, estimated_cost,
                    ):
                        db.rollback()
                        return None
                for limit in consumer_limits if self.tenant_quotas else ():
                    row = db.execute(
                        """SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(tokens), 0),
                        COALESCE(SUM(cost_usd), 0) FROM usage_events
                        WHERE consumer_id = ? AND created_at >= ? AND status != 'failed'""",
                        (consumer_id, now - limit.window_seconds),
                    ).fetchone()
                    if not self._within(
                        limit, int(row[0]), int(row[1]), float(row[2]),
                        estimated_tokens, estimated_cost,
                    ):
                        db.rollback()
                        return None
                db.execute(
                    """INSERT INTO usage_events
                    (provider, request_id, created_at, requests, tokens, status, model,
                     cost_usd, consumer_id)
                    VALUES (?, ?, ?, 1, ?, 'reserved', ?, ?, ?)""",
                    (provider.name, request_id, now, estimated_tokens, model,
                     estimated_cost, consumer_id),
                )
                db.commit()
        return Reservation(
            provider.name, request_id, estimated_tokens, model,
                    input_cost_per_million if self.cost_tracking else 0,
                    output_cost_per_million if self.cost_tracking else 0,
        )

    async def reconcile(
        self, reservation: Reservation, actual_tokens: int | None, success: bool,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> None:
        async with self._lock:
            with self._connect() as db:
                if success:
                    prompt = int(prompt_tokens or 0)
                    completion = int(completion_tokens or 0)
                    cost = (
                        prompt * reservation.input_cost_per_million
                        + completion * reservation.output_cost_per_million
                    ) / 1_000_000
                    if prompt + completion == 0:
                        cost = (actual_tokens or reservation.estimated_tokens) * max(
                            reservation.input_cost_per_million,
                            reservation.output_cost_per_million,
                        ) / 1_000_000
                    db.execute(
                        """UPDATE usage_events SET tokens = ?, status = 'completed',
                        prompt_tokens = ?, completion_tokens = ?, cost_usd = ?
                        WHERE request_id = ?""",
                        (actual_tokens or reservation.estimated_tokens, prompt, completion, cost,
                         reservation.request_id),
                    )
                else:
                    db.execute(
                        "UPDATE usage_events SET requests = 0, tokens = 0, status = 'failed' WHERE request_id = ?",
                        (reservation.request_id,),
                    )

    async def snapshot(self) -> list[dict[str, object]]:
        async with self._lock:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT provider, SUM(requests), SUM(tokens), MAX(created_at),
                    SUM(prompt_tokens), SUM(completion_tokens), SUM(cost_usd)
                    FROM usage_events WHERE status != 'failed' GROUP BY provider"""
                ).fetchall()
        return [
            {"provider": row[0], "requests": row[1], "tokens": row[2], "last_used_at": row[3],
             "prompt_tokens": row[4], "completion_tokens": row[5], "cost_usd": round(row[6], 8)}
            for row in rows
        ]

    async def consumer_snapshot(self) -> list[dict[str, object]]:
        async with self._lock:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT consumer_id, SUM(requests), SUM(tokens), SUM(cost_usd),
                    MAX(created_at) FROM usage_events
                    WHERE status != 'failed' AND consumer_id IS NOT NULL GROUP BY consumer_id"""
                ).fetchall()
        return [{
            "tenant_id": row[0], "requests": row[1], "tokens": row[2],
            "cost_usd": round(row[3], 8), "last_used_at": row[4],
        } for row in rows]

    async def usage_export(
        self, after: float = 0, after_id: str = "", limit: int = 1000,
    ) -> list[dict[str, object]]:
        async with self._lock:
            with self._connect() as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    """SELECT request_id,created_at,provider,model,consumer_id tenant_id,
                    requests,tokens,prompt_tokens,completion_tokens,cost_usd,status
                    FROM usage_events WHERE status='completed'
                    AND (created_at>? OR (created_at=? AND request_id>?))
                    ORDER BY created_at,request_id LIMIT ?""", (after, after, after_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    async def export_checkpoint(self, sink_id: str) -> tuple[float, str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT after_timestamp,after_request_id FROM usage_export_checkpoints WHERE sink_id=?",
                (sink_id,),
            ).fetchone()
        return (float(row[0]), str(row[1])) if row else (0, "")

    async def save_export_checkpoint(
        self, sink_id: str, after: float, after_id: str, page_sha256: str,
    ) -> None:
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO usage_export_checkpoints
                    (sink_id,after_timestamp,after_request_id,page_sha256,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(sink_id) DO UPDATE SET
                    after_timestamp=excluded.after_timestamp,
                    after_request_id=excluded.after_request_id,
                    page_sha256=excluded.page_sha256,updated_at=excluded.updated_at""",
                    (sink_id, after, after_id, page_sha256, time.time()),
                )

    async def cleanup(self, before_timestamp: float) -> int:
        async with self._lock:
            with self._connect() as db:
                cursor = db.execute(
                    "DELETE FROM usage_events WHERE created_at < ? AND status!='completed'",
                    (before_timestamp,),
                )
        return cursor.rowcount


class NullQuotaStore:
    """No-I/O quota implementation for the minimal single-user hot path."""

    async def initialize(self) -> None:
        return None

    async def reserve(
        self, provider: ProviderConfig, request_id: str, estimated_tokens: int,
        model: str | None = None, input_cost_per_million: float = 0,
        output_cost_per_million: float = 0, consumer_id: str | None = None,
        consumer_limits: tuple[Limit, ...] = (),
    ) -> Reservation:
        return Reservation(
            provider.name, request_id, estimated_tokens, model,
            input_cost_per_million, output_cost_per_million, False,
        )

    async def reconcile(self, reservation: Reservation, actual_tokens: int | None, success: bool,
                        prompt_tokens: int | None = None, completion_tokens: int | None = None) -> None:
        return None

    async def snapshot(self) -> list[dict[str, object]]:
        return []

    async def consumer_snapshot(self) -> list[dict[str, object]]:
        return []

    async def usage_export(
        self, after: float = 0, after_id: str = "", limit: int = 1000,
    ) -> list[dict[str, object]]:
        return []

    async def export_checkpoint(self, sink_id: str) -> tuple[float, str]:
        return 0, ""

    async def save_export_checkpoint(
        self, sink_id: str, after: float, after_id: str, page_sha256: str,
    ) -> None:
        return None

    async def cleanup(self, before_timestamp: float) -> int:
        return 0


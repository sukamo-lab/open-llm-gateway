from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable

from .migrations import migrate_sqlite


FINAL_STATES = {"succeeded", "failed", "cancelled"}
JobHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]
JobNotifier = Callable[[dict[str, object]], Awaitable[None]]


def _claimed(row) -> dict[str, object]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


class SQLiteJobStore:
    def __init__(self, database: str):
        self.database = database
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            migrate_sqlite(self.database, ("jobs",))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    async def create(self, tenant_id: str, kind: str, payload: dict[str, object]) -> dict[str, object]:
        job_id, now = str(uuid.uuid4()), time.time()
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO async_jobs
                    (id,tenant_id,kind,state,payload_json,created_at,updated_at,cancel_requested)
                    VALUES(?,?,?,'queued',?,?,?,0)""",
                    (job_id, tenant_id, kind, json.dumps(payload, separators=(",", ":")), now, now),
                )
        return await self.get(tenant_id, job_id)  # type: ignore[return-value]

    async def get(self, tenant_id: str, job_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT id,tenant_id,kind,state,created_at,updated_at,cancel_requested,
                error_code,result_json FROM async_jobs WHERE id=? AND tenant_id=?""", (job_id, tenant_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        encoded = result.pop("result_json")
        result["result"] = json.loads(encoded) if encoded else None
        return result

    async def cancel(self, tenant_id: str, job_id: str) -> dict[str, object] | None:
        async with self._lock:
            with self._connect() as db:
                row = db.execute(
                    "SELECT state FROM async_jobs WHERE id=? AND tenant_id=?", (job_id, tenant_id),
                ).fetchone()
                if not row:
                    return None
                if row["state"] not in FINAL_STATES:
                    db.execute(
                        """UPDATE async_jobs SET state='cancelled',cancel_requested=1,updated_at=?
                        WHERE id=? AND tenant_id=?""", (time.time(), job_id, tenant_id),
                    )
        return await self.get(tenant_id, job_id)

    async def claim(
        self, worker_id: str, lease_seconds: int, kinds: tuple[str, ...] = (),
    ) -> dict[str, object] | None:
        now = time.time()
        kind_filter = ""
        parameters: tuple[object, ...] = (now,)
        if kinds:
            kind_filter = f" AND kind IN ({','.join('?' for _ in kinds)})"
            parameters = (now, *kinds)
        async with self._lock:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    f"""SELECT * FROM async_jobs WHERE cancel_requested=0 AND
                    (state='queued' OR (state='running' AND lease_until<?))
                    {kind_filter} ORDER BY created_at,id LIMIT 1""", parameters,
                ).fetchone()
                if row is None:
                    db.rollback()
                    return None
                db.execute(
                    """UPDATE async_jobs SET state='running',lease_owner=?,lease_until=?,
                    attempts=attempts+1,updated_at=? WHERE id=?""",
                    (worker_id, now + lease_seconds, now, row["id"]),
                )
                claimed = db.execute("SELECT * FROM async_jobs WHERE id=?", (row["id"],)).fetchone()
                db.commit()
        return _claimed(claimed)

    async def complete(
        self, job_id: str, worker_id: str, result: dict[str, object] | None = None,
        error_code: str | None = None, notification: dict[str, object] | None = None,
    ) -> bool:
        state = "failed" if error_code else "succeeded"
        async with self._lock:
            with self._connect() as db:
                cursor = db.execute(
                    """UPDATE async_jobs SET state=?,result_json=?,error_code=?,updated_at=?,
                    lease_owner=NULL,lease_until=NULL WHERE id=? AND state='running'
                    AND lease_owner=? AND cancel_requested=0""",
                    (state, json.dumps(result) if result is not None else None, error_code,
                     time.time(), job_id, worker_id),
                )
                if cursor.rowcount == 1 and notification is not None:
                    now = time.time()
                    db.execute(
                        """INSERT INTO job_webhook_deliveries
                        (job_id,event_json,state,attempts,next_attempt_at,updated_at)
                        VALUES(?,?,'pending',0,?,?) ON CONFLICT(job_id) DO NOTHING""",
                        (job_id, json.dumps(notification, sort_keys=True, separators=(",", ":")), now, now),
                    )
        return cursor.rowcount == 1

    async def claim_notification(self, worker_id: str, lease_seconds: int) -> dict[str, object] | None:
        now = time.time()
        async with self._lock:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """SELECT * FROM job_webhook_deliveries WHERE
                    (state='pending' AND next_attempt_at<=?) OR
                    (state='delivering' AND lease_until<?)
                    ORDER BY next_attempt_at,job_id LIMIT 1""", (now, now),
                ).fetchone()
                if row is None:
                    db.rollback()
                    return None
                db.execute(
                    """UPDATE job_webhook_deliveries SET state='delivering',lease_owner=?,
                    lease_until=?,attempts=attempts+1,updated_at=? WHERE job_id=?""",
                    (worker_id, now + lease_seconds, now, row["job_id"]),
                )
                claimed = db.execute(
                    "SELECT * FROM job_webhook_deliveries WHERE job_id=?", (row["job_id"],),
                ).fetchone()
                db.commit()
        item = dict(claimed)
        item["event"] = json.loads(item.pop("event_json"))
        return item

    async def finish_notification(
        self, job_id: str, worker_id: str, delivered: bool, retry_delay: float = 0,
        error_code: str | None = None,
    ) -> bool:
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE job_webhook_deliveries SET state=?,next_attempt_at=?,last_error=?,
                lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?
                AND state='delivering' AND lease_owner=?""",
                ("delivered" if delivered else "pending", now + retry_delay,
                 error_code, now, job_id, worker_id),
            )
        return cursor.rowcount == 1

    async def close(self) -> None:
        return None


class PostgresJobStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def initialize(self) -> None:
        import asyncpg
        from .migrations import migrate_postgres

        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        await migrate_postgres(self.pool, ("jobs",))

    async def create(self, tenant_id: str, kind: str, payload: dict[str, object]) -> dict[str, object]:
        job_id, now = str(uuid.uuid4()), time.time()
        await self.pool.execute(
            """INSERT INTO async_jobs
            (id,tenant_id,kind,state,payload_json,created_at,updated_at,cancel_requested)
            VALUES($1,$2,$3,'queued',$4,$5,$5,FALSE)""",
            job_id, tenant_id, kind, json.dumps(payload, separators=(",", ":")), now,
        )
        return await self.get(tenant_id, job_id)  # type: ignore[return-value]

    async def get(self, tenant_id: str, job_id: str) -> dict[str, object] | None:
        row = await self.pool.fetchrow(
            """SELECT id,tenant_id,kind,state,created_at,updated_at,cancel_requested,error_code,result_json
            FROM async_jobs WHERE id=$1 AND tenant_id=$2""", job_id, tenant_id,
        )
        if not row:
            return None
        result = dict(row)
        encoded = result.pop("result_json")
        result["result"] = json.loads(encoded) if encoded else None
        return result

    async def cancel(self, tenant_id: str, job_id: str) -> dict[str, object] | None:
        result = await self.pool.execute(
            """UPDATE async_jobs SET state='cancelled',cancel_requested=TRUE,updated_at=$1
            WHERE id=$2 AND tenant_id=$3 AND state IN ('queued','running')""",
            time.time(), job_id, tenant_id,
        )
        if result.endswith("0") and await self.get(tenant_id, job_id) is None:
            return None
        return await self.get(tenant_id, job_id)

    async def claim(
        self, worker_id: str, lease_seconds: int, kinds: tuple[str, ...] = (),
    ) -> dict[str, object] | None:
        now = time.time()
        row = await self.pool.fetchrow(
            """WITH candidate AS (
              SELECT id FROM async_jobs WHERE NOT cancel_requested AND
              (state='queued' OR (state='running' AND lease_until<$1))
              AND (cardinality($4::text[])=0 OR kind=ANY($4::text[]))
              ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE async_jobs j SET state='running',lease_owner=$2,lease_until=$3,
              attempts=j.attempts+1,updated_at=$1 FROM candidate c WHERE j.id=c.id
            RETURNING j.*""", now, worker_id, now + lease_seconds, list(kinds),
        )
        return _claimed(row) if row else None

    async def complete(
        self, job_id: str, worker_id: str, result: dict[str, object] | None = None,
        error_code: str | None = None, notification: dict[str, object] | None = None,
    ) -> bool:
        async with self.pool.acquire() as connection, connection.transaction():
            now = time.time()
            command = await connection.execute(
                """UPDATE async_jobs SET state=$1,result_json=$2,error_code=$3,updated_at=$4,
                lease_owner=NULL,lease_until=NULL WHERE id=$5 AND state='running'
                AND lease_owner=$6 AND NOT cancel_requested""",
                "failed" if error_code else "succeeded",
                json.dumps(result) if result is not None else None,
                error_code, now, job_id, worker_id,
            )
            if command.endswith("1") and notification is not None:
                await connection.execute(
                    """INSERT INTO job_webhook_deliveries
                    (job_id,event_json,state,attempts,next_attempt_at,updated_at)
                    VALUES($1,$2,'pending',0,$3,$3) ON CONFLICT(job_id) DO NOTHING""",
                    job_id, json.dumps(notification, sort_keys=True, separators=(",", ":")), now,
                )
        return command.endswith("1")

    async def claim_notification(self, worker_id: str, lease_seconds: int) -> dict[str, object] | None:
        now = time.time()
        row = await self.pool.fetchrow(
            """WITH candidate AS (
              SELECT job_id FROM job_webhook_deliveries WHERE
              (state='pending' AND next_attempt_at<=$1) OR
              (state='delivering' AND lease_until<$1)
              ORDER BY next_attempt_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE job_webhook_deliveries d SET state='delivering',lease_owner=$2,
              lease_until=$3,attempts=d.attempts+1,updated_at=$1 FROM candidate c
              WHERE d.job_id=c.job_id RETURNING d.*""",
            now, worker_id, now + lease_seconds,
        )
        if not row:
            return None
        item = dict(row)
        item["event"] = json.loads(item.pop("event_json"))
        return item

    async def finish_notification(
        self, job_id: str, worker_id: str, delivered: bool, retry_delay: float = 0,
        error_code: str | None = None,
    ) -> bool:
        now = time.time()
        command = await self.pool.execute(
            """UPDATE job_webhook_deliveries SET state=$1,next_attempt_at=$2,last_error=$3,
            lease_owner=NULL,lease_until=NULL,updated_at=$4 WHERE job_id=$5
            AND state='delivering' AND lease_owner=$6""",
            "delivered" if delivered else "pending", now + retry_delay,
            error_code, now, job_id, worker_id,
        )
        return command.endswith("1")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()


class JobWorker:
    def __init__(
        self, store, handlers: dict[str, JobHandler], worker_id: str | None = None,
        lease_seconds: int = 60, notifier: JobNotifier | None = None,
        restrict_to_handlers: bool = False,
    ):
        self.store = store
        self.handlers = handlers
        self.worker_id = worker_id or str(uuid.uuid4())
        self.lease_seconds = lease_seconds
        self.notifier = notifier
        self.restrict_to_handlers = restrict_to_handlers

    async def run_once(self) -> bool:
        kinds = tuple(self.handlers) if self.restrict_to_handlers else ()
        job = await self.store.claim(self.worker_id, self.lease_seconds, kinds)
        if job is None:
            return False
        handler = self.handlers.get(str(job["kind"]))
        if handler is None:
            event = self._event(job, "failed") if self.notifier else None
            await self.store.complete(
                str(job["id"]), self.worker_id, error_code="unknown_job_kind", notification=event,
            )
            return True
        try:
            result = await handler(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            event = self._event(job, "failed") if self.notifier else None
            await self.store.complete(
                str(job["id"]), self.worker_id, error_code="handler_failed", notification=event,
            )
        else:
            event = self._event(job, "succeeded") if self.notifier else None
            await self.store.complete(
                str(job["id"]), self.worker_id, result=result, notification=event,
            )
        return True

    @staticmethod
    def _event(job: dict[str, object], state: str) -> dict[str, object]:
        return {
            "id": job["id"], "tenant_id": job["tenant_id"],
            "kind": job["kind"], "state": state,
        }


class WebhookDeliveryWorker:
    def __init__(
        self, store, notifier: JobNotifier, worker_id: str | None = None,
        lease_seconds: int = 60, max_retry_seconds: int = 3600,
    ):
        self.store, self.notifier = store, notifier
        self.worker_id = worker_id or str(uuid.uuid4())
        self.lease_seconds, self.max_retry_seconds = lease_seconds, max_retry_seconds

    async def run_once(self) -> bool:
        delivery = await self.store.claim_notification(self.worker_id, self.lease_seconds)
        if delivery is None:
            return False
        try:
            await self.notifier(delivery["event"])
        except Exception:
            delay = min(2 ** min(int(delivery["attempts"]), 12), self.max_retry_seconds)
            await self.store.finish_notification(
                str(delivery["job_id"]), self.worker_id, False, delay, "delivery_failed",
            )
        else:
            await self.store.finish_notification(
                str(delivery["job_id"]), self.worker_id, True,
            )
        return True


class SignedWebhookNotifier:
    def __init__(
        self, url: str, secret: str, client, attempts: int = 3,
        timeout_seconds: float = 10,
    ):
        self.url, self.secret, self.client = url, secret.encode(), client
        self.attempts, self.timeout_seconds = attempts, timeout_seconds

    async def __call__(self, event: dict[str, object]) -> None:
        body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new(self.secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Gateway-Signature": f"t={timestamp},v1={digest}",
            "Idempotency-Key": str(event["id"]),
        }
        for attempt in range(self.attempts):
            try:
                response = await self.client.post(
                    self.url, content=body, headers=headers, timeout=self.timeout_seconds,
                )
                if 200 <= response.status_code < 300:
                    return
            except Exception:
                pass
            if attempt + 1 < self.attempts:
                await asyncio.sleep(min(2 ** attempt, 5))
        raise RuntimeError("job webhook delivery failed")

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable


LATEST_VERSIONS = {"usage": 1, "identity": 1, "idempotency": 1, "cache": 1}


def _validate_components(components: Iterable[str]) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(components))
    unknown = set(result) - set(LATEST_VERSIONS)
    if unknown:
        raise ValueError(f"unknown database component(s): {', '.join(sorted(unknown))}")
    return result


def _sqlite_history(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        component TEXT PRIMARY KEY, version INTEGER NOT NULL,
        applied_at REAL NOT NULL)"""
    )


def _sqlite_usage_v1(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS usage_events (
        provider TEXT NOT NULL, request_id TEXT PRIMARY KEY,
        created_at REAL NOT NULL, requests INTEGER NOT NULL,
        tokens INTEGER NOT NULL, status TEXT NOT NULL,
        model TEXT, prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0, consumer_id TEXT)"""
    )
    existing = {row[1] for row in db.execute("PRAGMA table_info(usage_events)")}
    additions = {
        "model": "TEXT",
        "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
        "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cost_usd": "REAL NOT NULL DEFAULT 0",
        "consumer_id": "TEXT",
    }
    for column, definition in additions.items():
        if column not in existing:
            db.execute(f"ALTER TABLE usage_events ADD COLUMN {column} {definition}")
    db.execute("CREATE INDEX IF NOT EXISTS usage_provider_time ON usage_events(provider, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS usage_consumer_time ON usage_events(consumer_id, created_at)")


def _sqlite_identity_v1(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          active INTEGER NOT NULL DEFAULT 1, requests_per_minute INTEGER,
          tokens_per_day INTEGER, cost_per_30_days REAL, created_at REAL NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS consumer_keys (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
          key_prefix TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
          active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
          last_used_at REAL, expires_at REAL,
          FOREIGN KEY(tenant_id) REFERENCES tenants(id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS consumer_keys_hash ON consumer_keys(key_hash)")
    db.execute("CREATE INDEX IF NOT EXISTS consumer_keys_tenant ON consumer_keys(tenant_id)")
    columns = {row[1] for row in db.execute("PRAGMA table_info(consumer_keys)")}
    if "expires_at" not in columns:
        db.execute("ALTER TABLE consumer_keys ADD COLUMN expires_at REAL")


def _sqlite_idempotency_v1(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS idempotency_records (
        tenant_id TEXT NOT NULL, key_hash TEXT NOT NULL,
        request_hash TEXT NOT NULL, owner_token TEXT NOT NULL,
        state TEXT NOT NULL, response_json TEXT,
        created_at REAL NOT NULL, expires_at REAL NOT NULL,
        lease_until REAL NOT NULL,
        PRIMARY KEY(tenant_id, key_hash))"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idempotency_expiry ON idempotency_records(expires_at)"
    )


def _sqlite_cache_v1(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS exact_cache_entries (
        tenant_id TEXT NOT NULL, cache_key TEXT NOT NULL,
        response_json TEXT NOT NULL, created_at REAL NOT NULL,
        expires_at REAL NOT NULL, PRIMARY KEY(tenant_id,cache_key))"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS exact_cache_expiry ON exact_cache_entries(expires_at)")
    db.execute("CREATE INDEX IF NOT EXISTS exact_cache_created ON exact_cache_entries(created_at)")


SQLITE_MIGRATIONS = {
    "usage": {1: _sqlite_usage_v1},
    "identity": {1: _sqlite_identity_v1},
    "idempotency": {1: _sqlite_idempotency_v1},
    "cache": {1: _sqlite_cache_v1},
}


def migrate_sqlite(database: str, components: Iterable[str]) -> dict[str, int]:
    selected = _validate_components(components)
    if not selected:
        return {}
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    before = sqlite_status(database, selected)
    if database_path.exists() and any(before[item] < LATEST_VERSIONS[item] for item in selected):
        backup = database_path.with_name(f"{database_path.name}.pre-migration-{time.time_ns()}.bak")
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
    with sqlite3.connect(database, timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")
        _sqlite_history(db)
        for component in selected:
            row = db.execute(
                "SELECT version FROM schema_migrations WHERE component = ?", (component,)
            ).fetchone()
            current = int(row[0]) if row else 0
            latest = LATEST_VERSIONS[component]
            if current > latest:
                raise RuntimeError(
                    f"database {component} schema version {current} is newer than supported {latest}"
                )
            for version in range(current + 1, latest + 1):
                SQLITE_MIGRATIONS[component][version](db)
                db.execute(
                    """INSERT INTO schema_migrations(component,version,applied_at)
                    VALUES(?,?,?) ON CONFLICT(component) DO UPDATE SET
                    version=excluded.version, applied_at=excluded.applied_at""",
                    (component, version, time.time()),
                )
        return sqlite_status_connection(db, selected)


def sqlite_status(database: str, components: Iterable[str]) -> dict[str, int]:
    selected = _validate_components(components)
    if not selected or not Path(database).exists():
        return {component: 0 for component in selected}
    with sqlite3.connect(database, timeout=30) as db:
        return sqlite_status_connection(db, selected)


def sqlite_status_connection(db: sqlite3.Connection, components: Iterable[str]) -> dict[str, int]:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return {component: 0 for component in components}
    rows = dict(db.execute("SELECT component,version FROM schema_migrations"))
    return {component: int(rows.get(component, 0)) for component in components}


async def migrate_postgres(pool, components: Iterable[str]) -> dict[str, int]:
    selected = _validate_components(components)
    if not selected:
        return {}
    async with pool.acquire() as db, db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock(hashtext('llm-gateway-schema'))")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
            component TEXT PRIMARY KEY, version INTEGER NOT NULL,
            applied_at DOUBLE PRECISION NOT NULL)"""
        )
        for component in selected:
            current = await db.fetchval(
                "SELECT version FROM schema_migrations WHERE component=$1", component
            ) or 0
            latest = LATEST_VERSIONS[component]
            if current > latest:
                raise RuntimeError(
                    f"database {component} schema version {current} is newer than supported {latest}"
                )
            for version in range(current + 1, latest + 1):
                await _postgres_migration(db, component, version)
                await db.execute(
                    """INSERT INTO schema_migrations(component,version,applied_at)
                    VALUES($1,$2,$3) ON CONFLICT(component) DO UPDATE SET
                    version=EXCLUDED.version, applied_at=EXCLUDED.applied_at""",
                    component, version, time.time(),
                )
        return await postgres_status_connection(db, selected)


async def _postgres_migration(db, component: str, version: int) -> None:
    if (component, version) == ("usage", 1):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS usage_events (
              provider TEXT NOT NULL, request_id TEXT PRIMARY KEY,
              created_at DOUBLE PRECISION NOT NULL, requests INTEGER NOT NULL,
              tokens BIGINT NOT NULL, status TEXT NOT NULL, model TEXT,
              prompt_tokens BIGINT NOT NULL DEFAULT 0,
              completion_tokens BIGINT NOT NULL DEFAULT 0,
              cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, consumer_id TEXT
            );
            ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS model TEXT;
            ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS prompt_tokens BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS completion_tokens BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
            ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS consumer_id TEXT;
            CREATE INDEX IF NOT EXISTS usage_provider_time ON usage_events(provider, created_at);
            CREATE INDEX IF NOT EXISTS usage_consumer_time ON usage_events(consumer_id, created_at);"""
        )
    elif (component, version) == ("identity", 1):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS tenants (
              id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
              active BOOLEAN NOT NULL DEFAULT TRUE, requests_per_minute INTEGER,
              tokens_per_day BIGINT, cost_per_30_days DOUBLE PRECISION,
              created_at DOUBLE PRECISION NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumer_keys (
              id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
              name TEXT NOT NULL, key_prefix TEXT NOT NULL,
              key_hash TEXT NOT NULL UNIQUE, active BOOLEAN NOT NULL DEFAULT TRUE,
              created_at DOUBLE PRECISION NOT NULL, last_used_at DOUBLE PRECISION,
              expires_at DOUBLE PRECISION
            );
            ALTER TABLE consumer_keys ADD COLUMN IF NOT EXISTS expires_at DOUBLE PRECISION;
            CREATE INDEX IF NOT EXISTS consumer_keys_hash ON consumer_keys(key_hash);
            CREATE INDEX IF NOT EXISTS consumer_keys_tenant ON consumer_keys(tenant_id);"""
        )
    elif (component, version) == ("idempotency", 1):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS idempotency_records (
              tenant_id TEXT NOT NULL, key_hash TEXT NOT NULL,
              request_hash TEXT NOT NULL, owner_token TEXT NOT NULL,
              state TEXT NOT NULL, response_json TEXT,
              created_at DOUBLE PRECISION NOT NULL,
              expires_at DOUBLE PRECISION NOT NULL,
              lease_until DOUBLE PRECISION NOT NULL,
              PRIMARY KEY(tenant_id, key_hash)
            );
            CREATE INDEX IF NOT EXISTS idempotency_expiry
              ON idempotency_records(expires_at);"""
        )
    elif (component, version) == ("cache", 1):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS exact_cache_entries (
              tenant_id TEXT NOT NULL, cache_key TEXT NOT NULL,
              response_json TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
              expires_at DOUBLE PRECISION NOT NULL,
              PRIMARY KEY(tenant_id,cache_key)
            );
            CREATE INDEX IF NOT EXISTS exact_cache_expiry
              ON exact_cache_entries(expires_at);
            CREATE INDEX IF NOT EXISTS exact_cache_created
              ON exact_cache_entries(created_at);"""
        )
    else:
        raise RuntimeError(f"missing PostgreSQL migration {component} version {version}")


async def postgres_status(pool, components: Iterable[str]) -> dict[str, int]:
    selected = _validate_components(components)
    if not selected:
        return {}
    async with pool.acquire() as db:
        exists = await db.fetchval("SELECT to_regclass('schema_migrations')")
        if not exists:
            return {component: 0 for component in selected}
        return await postgres_status_connection(db, selected)


async def postgres_status_connection(db, components: Iterable[str]) -> dict[str, int]:
    rows = await db.fetch("SELECT component,version FROM schema_migrations")
    versions = {row["component"]: int(row["version"]) for row in rows}
    return {component: versions.get(component, 0) for component in components}

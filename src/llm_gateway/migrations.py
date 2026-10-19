from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable


LATEST_VERSIONS = {"usage": 3, "identity": 5, "idempotency": 1, "cache": 1, "audit": 1, "jobs": 2, "coordination": 1}


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


def _sqlite_usage_v2(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS usage_export_checkpoints (
        sink_id TEXT PRIMARY KEY, after_timestamp REAL NOT NULL,
        after_request_id TEXT NOT NULL, page_sha256 TEXT NOT NULL,
        updated_at REAL NOT NULL)"""
    )


def _sqlite_usage_v3(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS usage_completed_no_update
        BEFORE UPDATE ON usage_events WHEN OLD.status='completed'
        BEGIN SELECT RAISE(ABORT,'completed usage events are immutable'); END"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS usage_completed_no_delete
        BEFORE DELETE ON usage_events WHEN OLD.status='completed'
        BEGIN SELECT RAISE(ABORT,'completed usage events are immutable'); END"""
    )


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


def _sqlite_identity_v2(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(
        """CREATE TABLE IF NOT EXISTS organizations (
        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL)"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
        UNIQUE(organization_id,name),
        FOREIGN KEY(organization_id) REFERENCES organizations(id))"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS principals (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, project_id TEXT,
        kind TEXT NOT NULL CHECK(kind IN ('user','service_account')),
        subject TEXT NOT NULL, display_name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
        UNIQUE(organization_id,kind,subject),
        FOREIGN KEY(organization_id) REFERENCES organizations(id),
        FOREIGN KEY(project_id) REFERENCES projects(id))"""
    )
    tenant_columns = {row[1] for row in db.execute("PRAGMA table_info(tenants)")}
    if "organization_id" not in tenant_columns:
        db.execute("ALTER TABLE tenants ADD COLUMN organization_id TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS projects_organization ON projects(organization_id)")
    db.execute("CREATE INDEX IF NOT EXISTS principals_organization ON principals(organization_id)")
    db.execute("CREATE INDEX IF NOT EXISTS principals_project ON principals(project_id)")
    db.execute("CREATE INDEX IF NOT EXISTS tenants_organization ON tenants(organization_id)")


def _sqlite_identity_v3(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(consumer_keys)")}
    for column, definition in {
        "principal_id": "TEXT", "project_id": "TEXT", "permission_ceiling": "TEXT",
    }.items():
        if column not in columns:
            db.execute(f"ALTER TABLE consumer_keys ADD COLUMN {column} {definition}")
    db.execute(
        """CREATE TABLE IF NOT EXISTS role_bindings (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, principal_id TEXT NOT NULL,
        role_name TEXT NOT NULL, project_id TEXT, created_at REAL NOT NULL,
        UNIQUE(principal_id,role_name,project_id),
        FOREIGN KEY(organization_id) REFERENCES organizations(id),
        FOREIGN KEY(principal_id) REFERENCES principals(id),
        FOREIGN KEY(project_id) REFERENCES projects(id))"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS role_bindings_principal ON role_bindings(principal_id)")
    db.execute("CREATE INDEX IF NOT EXISTS consumer_keys_principal ON consumer_keys(principal_id)")


def _sqlite_identity_v4(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(tenants)")}
    if "plan_id" not in columns:
        db.execute("ALTER TABLE tenants ADD COLUMN plan_id TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS tenants_plan ON tenants(plan_id)")


def _sqlite_identity_v5(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS tenant_credit_grants (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, amount_usd REAL NOT NULL,
        source TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL,
        CHECK(amount_usd>0), FOREIGN KEY(tenant_id) REFERENCES tenants(id))"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS tenant_credits_active ON tenant_credit_grants(tenant_id,expires_at)"
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS tenant_credits_no_update BEFORE UPDATE
        ON tenant_credit_grants BEGIN SELECT RAISE(ABORT,'credit grants are append-only'); END"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS tenant_credits_no_delete BEFORE DELETE
        ON tenant_credit_grants BEGIN SELECT RAISE(ABORT,'credit grants are append-only'); END"""
    )


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


def _sqlite_audit_v1(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS audit_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        organization_id TEXT, actor_id TEXT NOT NULL, event_type TEXT NOT NULL,
        outcome TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at REAL NOT NULL,
        previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE)"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS audit_tenant_sequence ON audit_events(tenant_id,sequence)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS audit_export_checkpoints (
        sink_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, event_hash TEXT NOT NULL,
        updated_at REAL NOT NULL)"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END"""
    )


def _sqlite_jobs_v1(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS async_jobs (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL,
        state TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT,
        error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        lease_owner TEXT, lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        CHECK(state IN ('queued','running','succeeded','failed','cancelled')))"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS async_jobs_tenant_created ON async_jobs(tenant_id,created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS async_jobs_state_lease ON async_jobs(state,lease_until)")


def _sqlite_jobs_v2(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS job_webhook_deliveries (
        job_id TEXT PRIMARY KEY, event_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL, lease_owner TEXT, lease_until REAL,
        last_error TEXT, updated_at REAL NOT NULL,
        CHECK(state IN ('pending','delivering','delivered')),
        FOREIGN KEY(job_id) REFERENCES async_jobs(id))"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS job_webhooks_due
        ON job_webhook_deliveries(state,next_attempt_at,lease_until)"""
    )


def _sqlite_coordination_v1(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS provider_circuits (
        provider TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
        open_until REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL)"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS provider_concurrency_leases (
        token TEXT PRIMARY KEY, provider TEXT NOT NULL, expires_at REAL NOT NULL)"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS provider_leases_provider_expiry
        ON provider_concurrency_leases(provider,expires_at)"""
    )


SQLITE_MIGRATIONS = {
    "usage": {1: _sqlite_usage_v1, 2: _sqlite_usage_v2, 3: _sqlite_usage_v3},
    "identity": {
        1: _sqlite_identity_v1, 2: _sqlite_identity_v2, 3: _sqlite_identity_v3,
        4: _sqlite_identity_v4, 5: _sqlite_identity_v5,
    },
    "idempotency": {1: _sqlite_idempotency_v1},
    "cache": {1: _sqlite_cache_v1},
    "audit": {1: _sqlite_audit_v1},
    "jobs": {1: _sqlite_jobs_v1, 2: _sqlite_jobs_v2},
    "coordination": {1: _sqlite_coordination_v1},
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
    elif (component, version) == ("usage", 2):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS usage_export_checkpoints (
              sink_id TEXT PRIMARY KEY, after_timestamp DOUBLE PRECISION NOT NULL,
              after_request_id TEXT NOT NULL, page_sha256 TEXT NOT NULL,
              updated_at DOUBLE PRECISION NOT NULL
            );"""
        )
    elif (component, version) == ("usage", 3):
        await db.execute(
            """CREATE OR REPLACE FUNCTION reject_completed_usage_mutation() RETURNS trigger AS $$
            BEGIN
              IF OLD.status = 'completed' THEN
                RAISE EXCEPTION 'completed usage events are immutable';
              END IF;
              RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END; $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS usage_completed_no_mutation ON usage_events;
            CREATE TRIGGER usage_completed_no_mutation BEFORE UPDATE OR DELETE ON usage_events
              FOR EACH ROW EXECUTE FUNCTION reject_completed_usage_mutation();"""
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
    elif (component, version) == ("identity", 2):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS organizations (
              id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
              active BOOLEAN NOT NULL DEFAULT TRUE, created_at DOUBLE PRECISION NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
              name TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
              created_at DOUBLE PRECISION NOT NULL, UNIQUE(organization_id,name)
            );
            CREATE TABLE IF NOT EXISTS principals (
              id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
              project_id TEXT REFERENCES projects(id), kind TEXT NOT NULL,
              subject TEXT NOT NULL, display_name TEXT NOT NULL,
              active BOOLEAN NOT NULL DEFAULT TRUE, created_at DOUBLE PRECISION NOT NULL,
              UNIQUE(organization_id,kind,subject),
              CHECK(kind IN ('user','service_account'))
            );
            ALTER TABLE tenants ADD COLUMN IF NOT EXISTS organization_id TEXT REFERENCES organizations(id);
            CREATE INDEX IF NOT EXISTS projects_organization ON projects(organization_id);
            CREATE INDEX IF NOT EXISTS principals_organization ON principals(organization_id);
            CREATE INDEX IF NOT EXISTS principals_project ON principals(project_id);
            CREATE INDEX IF NOT EXISTS tenants_organization ON tenants(organization_id);"""
        )
    elif (component, version) == ("identity", 3):
        await db.execute(
            """ALTER TABLE consumer_keys ADD COLUMN IF NOT EXISTS principal_id TEXT REFERENCES principals(id);
            ALTER TABLE consumer_keys ADD COLUMN IF NOT EXISTS project_id TEXT REFERENCES projects(id);
            ALTER TABLE consumer_keys ADD COLUMN IF NOT EXISTS permission_ceiling TEXT;
            CREATE TABLE IF NOT EXISTS role_bindings (
              id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
              principal_id TEXT NOT NULL REFERENCES principals(id), role_name TEXT NOT NULL,
              project_id TEXT REFERENCES projects(id), created_at DOUBLE PRECISION NOT NULL,
              UNIQUE(principal_id,role_name,project_id)
            );
            CREATE INDEX IF NOT EXISTS role_bindings_principal ON role_bindings(principal_id);
            CREATE INDEX IF NOT EXISTS consumer_keys_principal ON consumer_keys(principal_id);"""
        )
    elif (component, version) == ("identity", 4):
        await db.execute(
            """ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_id TEXT;
            CREATE INDEX IF NOT EXISTS tenants_plan ON tenants(plan_id);"""
        )
    elif (component, version) == ("identity", 5):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS tenant_credit_grants (
              id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
              amount_usd DOUBLE PRECISION NOT NULL CHECK(amount_usd>0), source TEXT NOT NULL,
              created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION
            );
            CREATE INDEX IF NOT EXISTS tenant_credits_active
              ON tenant_credit_grants(tenant_id,expires_at);
            CREATE OR REPLACE FUNCTION reject_credit_grant_mutation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'credit grants are append-only'; END; $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS tenant_credits_no_mutation ON tenant_credit_grants;
            CREATE TRIGGER tenant_credits_no_mutation BEFORE UPDATE OR DELETE ON tenant_credit_grants
              FOR EACH ROW EXECUTE FUNCTION reject_credit_grant_mutation();"""
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
    elif (component, version) == ("audit", 1):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
              sequence BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL,
              organization_id TEXT, actor_id TEXT NOT NULL, event_type TEXT NOT NULL,
              outcome TEXT NOT NULL, metadata_json TEXT NOT NULL,
              created_at DOUBLE PRECISION NOT NULL, previous_hash TEXT NOT NULL,
              event_hash TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS audit_tenant_sequence ON audit_events(tenant_id,sequence);
            CREATE TABLE IF NOT EXISTS audit_export_checkpoints (
              sink_id TEXT PRIMARY KEY, sequence BIGINT NOT NULL, event_hash TEXT NOT NULL,
              updated_at DOUBLE PRECISION NOT NULL
            );
            CREATE OR REPLACE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'audit events are append-only'; END; $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;
            CREATE TRIGGER audit_events_no_update BEFORE UPDATE OR DELETE ON audit_events
              FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();"""
        )
    elif (component, version) == ("jobs", 1):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS async_jobs (
              id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL,
              state TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT,
              error_code TEXT, created_at DOUBLE PRECISION NOT NULL,
              updated_at DOUBLE PRECISION NOT NULL, lease_owner TEXT,
              lease_until DOUBLE PRECISION, cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
              attempts INTEGER NOT NULL DEFAULT 0,
              CHECK(state IN ('queued','running','succeeded','failed','cancelled'))
            );
            CREATE INDEX IF NOT EXISTS async_jobs_tenant_created ON async_jobs(tenant_id,created_at);
            CREATE INDEX IF NOT EXISTS async_jobs_state_lease ON async_jobs(state,lease_until);"""
        )
    elif (component, version) == ("jobs", 2):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS job_webhook_deliveries (
              job_id TEXT PRIMARY KEY REFERENCES async_jobs(id), event_json TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at DOUBLE PRECISION NOT NULL, lease_owner TEXT,
              lease_until DOUBLE PRECISION, last_error TEXT,
              updated_at DOUBLE PRECISION NOT NULL,
              CHECK(state IN ('pending','delivering','delivered'))
            );
            CREATE INDEX IF NOT EXISTS job_webhooks_due
              ON job_webhook_deliveries(state,next_attempt_at,lease_until);"""
        )
    elif (component, version) == ("coordination", 1):
        await db.execute(
            """CREATE TABLE IF NOT EXISTS provider_circuits (
              provider TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
              open_until DOUBLE PRECISION NOT NULL DEFAULT 0,
              updated_at DOUBLE PRECISION NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_concurrency_leases (
              token TEXT PRIMARY KEY, provider TEXT NOT NULL,
              expires_at DOUBLE PRECISION NOT NULL
            );
            CREATE INDEX IF NOT EXISTS provider_leases_provider_expiry
              ON provider_concurrency_leases(provider,expires_at);"""
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

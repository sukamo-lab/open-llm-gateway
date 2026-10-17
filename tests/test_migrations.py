import sqlite3

from llm_gateway.migrations import LATEST_VERSIONS, migrate_sqlite, sqlite_status
from llm_gateway.cli import storage_components
from llm_gateway.config import GatewayConfig


def test_sqlite_migrations_are_component_scoped_and_idempotent(tmp_path):
    database = str(tmp_path / "gateway.db")
    assert sqlite_status(database, ("usage", "identity")) == {"usage": 0, "identity": 0}

    assert migrate_sqlite(database, ("usage",)) == {"usage": LATEST_VERSIONS["usage"]}
    assert sqlite_status(database, ("usage", "identity")) == {"usage": 1, "identity": 0}
    assert migrate_sqlite(database, ("usage",)) == {"usage": 1}

    with sqlite3.connect(database) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "usage_events" in tables
    assert "tenants" not in tables

    assert migrate_sqlite(database, ("identity",)) == {"identity": LATEST_VERSIONS["identity"]}
    assert sqlite_status(database, ("usage", "identity")) == {
        "usage": 1, "identity": LATEST_VERSIONS["identity"],
    }


def test_identity_v2_migration_preserves_existing_tenants(tmp_path):
    database = str(tmp_path / "identity-v1.db")
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE schema_migrations(component TEXT PRIMARY KEY, version INTEGER, applied_at REAL)")
        db.execute("INSERT INTO schema_migrations VALUES('identity',1,1)")
        db.execute("""CREATE TABLE tenants (
            id TEXT PRIMARY KEY, name TEXT UNIQUE, active INTEGER,
            requests_per_minute INTEGER, tokens_per_day INTEGER,
            cost_per_30_days REAL, created_at REAL)""")
        db.execute("INSERT INTO tenants VALUES('t','legacy',1,NULL,NULL,NULL,1)")
        db.execute("""CREATE TABLE consumer_keys (
            id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, key_prefix TEXT,
            key_hash TEXT UNIQUE, active INTEGER, created_at REAL,
            last_used_at REAL, expires_at REAL)""")

    migrate_sqlite(database, ("identity",))

    with sqlite3.connect(database) as db:
        assert db.execute("SELECT id,name FROM tenants").fetchone() == ("t", "legacy")
        columns = {row[1] for row in db.execute("PRAGMA table_info(tenants)")}
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "organization_id" in columns
    assert {"organizations", "projects", "principals"} <= tables


def test_phase3_storage_components_are_included_in_operator_migrations(tmp_path):
    config = GatewayConfig.model_validate({
        "mode": "multi", "database": str(tmp_path / "gateway.db"),
        "features": {"identity_hierarchy": True, "audit_ledger": True},
        "providers": [{
            "name": "p", "base_url": "https://example.test/v1", "api_key_env": "PROVIDER_KEY",
            "models": [{"name": "m", "upstream_model": "m"}],
        }],
    })
    components = storage_components(config)
    assert "identity" in components
    assert "audit" in components


def test_sqlite_migration_adopts_legacy_tables_without_data_loss(tmp_path):
    database = str(tmp_path / "legacy.db")
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE usage_events (
            provider TEXT NOT NULL, request_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL, requests INTEGER NOT NULL,
            tokens INTEGER NOT NULL, status TEXT NOT NULL)"""
        )
        db.execute("INSERT INTO usage_events VALUES ('p','r',1,1,2,'completed')")

    migrate_sqlite(database, ("usage",))

    with sqlite3.connect(database) as db:
        row = db.execute("SELECT provider,request_id,tokens,cost_usd FROM usage_events").fetchone()
        columns = {item[1] for item in db.execute("PRAGMA table_info(usage_events)")}
    assert row == ("p", "r", 2, 0.0)
    assert {"model", "prompt_tokens", "completion_tokens", "cost_usd", "consumer_id"} <= columns

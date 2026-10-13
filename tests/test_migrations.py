import sqlite3

from llm_gateway.migrations import LATEST_VERSIONS, migrate_sqlite, sqlite_status


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

    assert migrate_sqlite(database, ("identity",)) == {"identity": 1}
    assert sqlite_status(database, ("usage", "identity")) == {"usage": 1, "identity": 1}


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

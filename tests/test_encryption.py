import sqlite3

import pytest

pytest.importorskip("cryptography")

from llm_gateway.encryption import EncryptionError, EnvelopeCipher, rotate_sqlite
from llm_gateway.cache import SQLiteCacheStore
from llm_gateway.migrations import migrate_sqlite


def key(byte: int) -> bytes:
    return bytes([byte]) * 32


def test_envelope_encryption_binds_context_and_key_version():
    cipher = EnvelopeCipher({"v1": key(1)}, "v1")
    value = cipher.encrypt("sensitive", "tenant:t:record:1")
    assert value.startswith("enc:v1:v1:")
    assert cipher.decrypt(value, "tenant:t:record:1") == "sensitive"
    with pytest.raises(EncryptionError, match="authentication"):
        cipher.decrypt(value, "tenant:other:record:1")


def test_rotation_is_resumable_and_requires_old_key(tmp_path):
    database = str(tmp_path / "rotation.db")
    migrate_sqlite(database, ("idempotency", "cache"))
    old = EnvelopeCipher({"v1": key(1)}, "v1")
    with sqlite3.connect(database) as db:
        for index in range(3):
            digest = f"key-{index}"
            value = old.encrypt(f'{{"value":{index}}}', f"idempotency:t:{digest}")
            db.execute("""INSERT INTO idempotency_records VALUES(
                't',?,?, 'owner','completed',?,1,9999999999,0)""", (digest, "request", value))
    rotating = EnvelopeCipher({"v1": key(1), "v2": key(2)}, "v2")
    assert rotate_sqlite(database, rotating, 2, False) == {"rotated": 2}
    assert rotate_sqlite(database, rotating, 2, False) == {"rotated": 1}
    assert rotate_sqlite(database, rotating, 2, False) == {"rotated": 0}
    with sqlite3.connect(database) as db:
        values = list(db.execute("SELECT key_hash,response_json FROM idempotency_records"))
    assert all(value.startswith("enc:v1:v2:") for _, value in values)
    assert all(rotating.decrypt(value, f"idempotency:t:{digest}") for digest, value in values)


def test_plaintext_migration_is_explicit(tmp_path):
    database = str(tmp_path / "plaintext.db")
    migrate_sqlite(database, ("cache",))
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO exact_cache_entries VALUES('t','k','{}',1,9999999999)")
    cipher = EnvelopeCipher({"v2": key(2)}, "v2")
    with pytest.raises(EncryptionError, match="explicit migration approval"):
        rotate_sqlite(database, cipher, 10, False)
    assert rotate_sqlite(database, cipher, 10, True) == {"rotated": 1}


@pytest.mark.asyncio
async def test_plaintext_reads_require_explicit_migration_window(tmp_path):
    database = str(tmp_path / "read-migration.db")
    migrate_sqlite(database, ("cache",))
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO exact_cache_entries VALUES('t','k','{\"ok\":true}',1,9999999999)")
    cipher = EnvelopeCipher({"v2": key(2)}, "v2")
    closed = SQLiteCacheStore(database, 60, 10, cipher, allow_plaintext_migration=False)
    with pytest.raises(RuntimeError, match="requires migration"):
        await closed.get("t", "k")
    migration = SQLiteCacheStore(database, 60, 10, cipher, allow_plaintext_migration=True)
    assert (await migration.get("t", "k")).response == {"ok": True}

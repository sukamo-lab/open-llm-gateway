import sqlite3

import pytest

from scripts.sqlite_backup import backup, restore, verify


def test_sqlite_backup_manifest_integrity_and_guarded_restore(tmp_path):
    source, archive, restored = (
        tmp_path / "source.db", tmp_path / "backup.db", tmp_path / "restored.db",
    )
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE items(value TEXT)")
        db.execute("INSERT INTO items VALUES('preserved')")
    manifest = backup(source, archive)
    assert verify(archive)["sha256"] == manifest["sha256"]
    with pytest.raises(ValueError, match="--confirm"):
        restore(archive, restored, False)
    restore(archive, restored, True)
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT value FROM items").fetchone()[0] == "preserved"

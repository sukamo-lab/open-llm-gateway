from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def backup(source: Path, destination: Path) -> dict[str, object]:
    if source.resolve() == destination.resolve():
        raise ValueError("source and destination must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
        source_db.backup(destination_db)
    with sqlite3.connect(destination) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"backup integrity check failed: {result}")
    manifest = {
        "schema_version": 1, "created_at": time.time(),
        "file": destination.name, "size": destination.stat().st_size,
        "sha256": digest(destination),
    }
    destination.with_suffix(destination.suffix + ".manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    return manifest


def verify(path: Path) -> dict[str, object]:
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["sha256"] != digest(path) or manifest["size"] != path.stat().st_size:
        raise RuntimeError("backup manifest mismatch")
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
    return manifest


def restore(source: Path, destination: Path, confirm: bool) -> None:
    if not confirm:
        raise ValueError("restore requires --confirm")
    verify(source)
    if destination.exists():
        raise FileExistsError("restore destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifiable LLM Gateway SQLite backup/restore")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("source", type=Path)
    create.add_argument("destination", type=Path)
    check = sub.add_parser("verify")
    check.add_argument("backup", type=Path)
    recover = sub.add_parser("restore")
    recover.add_argument("backup", type=Path)
    recover.add_argument("destination", type=Path)
    recover.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if args.command == "create":
        print(json.dumps(backup(args.source, args.destination), sort_keys=True))
    elif args.command == "verify":
        print(json.dumps(verify(args.backup), sort_keys=True))
    else:
        restore(args.backup, args.destination, args.confirm)
        print(json.dumps({"restored": str(args.destination)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

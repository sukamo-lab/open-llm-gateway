from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
from typing import Any


PREFIX = "enc:v1:"


class EncryptionError(ValueError):
    pass


class EnvelopeCipher:
    """AES-256-GCM envelope encryption with versioned KEKs and record-bound AAD."""

    def __init__(self, keys: dict[str, bytes], active_version: str):
        if active_version not in keys:
            raise EncryptionError("active encryption key version is unavailable")
        if any(len(value) != 32 for value in keys.values()):
            raise EncryptionError("encryption keys must decode to exactly 32 bytes")
        self._keys = dict(keys)
        self.active_version = active_version

    @classmethod
    def from_environment(cls, keys_env: str, active_version: str) -> "EnvelopeCipher":
        raw = os.getenv(keys_env)
        if not raw:
            raise EncryptionError(f"{keys_env} is required")
        try:
            values = json.loads(raw)
            keys = {str(version): base64.b64decode(value, validate=True) for version, value in values.items()}
        except Exception as exc:
            raise EncryptionError("encryption keyring must be a JSON object of base64 keys") from exc
        return cls(keys, active_version)

    @staticmethod
    def _aesgcm(key: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise EncryptionError("envelope encryption requires the 'security' package extra") from exc
        return AESGCM(key)

    def encrypt(self, plaintext: str, context: str) -> str:
        if not context:
            raise EncryptionError("encryption context is required")
        data_key, data_nonce, wrap_nonce = secrets.token_bytes(32), secrets.token_bytes(12), secrets.token_bytes(12)
        aad = context.encode("utf-8")
        ciphertext = self._aesgcm(data_key).encrypt(data_nonce, plaintext.encode("utf-8"), aad)
        wrapped = self._aesgcm(self._keys[self.active_version]).encrypt(
            wrap_nonce, data_key, f"{context}|dek|{self.active_version}".encode("utf-8")
        )
        envelope = {
            "alg": "AES-256-GCM", "key_version": self.active_version,
            "wrap_nonce": base64.b64encode(wrap_nonce).decode("ascii"),
            "wrapped_key": base64.b64encode(wrapped).decode("ascii"),
            "data_nonce": base64.b64encode(data_nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return f"{PREFIX}{self.active_version}:{encoded}"

    @staticmethod
    def _payload(value: str) -> tuple[str, str]:
        try:
            prefix, format_version, key_version, encoded = value.split(":", 3)
        except ValueError as exc:
            raise EncryptionError("invalid encryption envelope") from exc
        if f"{prefix}:{format_version}:" != PREFIX or not key_version or not encoded:
            raise EncryptionError("invalid encryption envelope")
        return key_version, encoded

    def decrypt(self, value: str, context: str) -> str:
        if not value.startswith(PREFIX):
            raise EncryptionError("persisted value is not an encryption envelope")
        try:
            prefix_version, encoded = self._payload(value)
            envelope: dict[str, Any] = json.loads(base64.urlsafe_b64decode(encoded))
            version = envelope["key_version"]
            if version != prefix_version:
                raise EncryptionError("envelope key version mismatch")
            key = self._keys[version]
            wrap_nonce = base64.b64decode(envelope["wrap_nonce"], validate=True)
            wrapped = base64.b64decode(envelope["wrapped_key"], validate=True)
            data_nonce = base64.b64decode(envelope["data_nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            aad = context.encode("utf-8")
            data_key = self._aesgcm(key).decrypt(
                wrap_nonce, wrapped, f"{context}|dek|{version}".encode("utf-8")
            )
            return self._aesgcm(data_key).decrypt(data_nonce, ciphertext, aad).decode("utf-8")
        except EncryptionError:
            raise
        except Exception as exc:
            raise EncryptionError("envelope authentication or decoding failed") from exc

    def key_version(self, value: str) -> str:
        try:
            prefix_version, encoded = self._payload(value)
            envelope = json.loads(base64.urlsafe_b64decode(encoded))
            if str(envelope["key_version"]) != prefix_version:
                raise EncryptionError("envelope key version mismatch")
            return prefix_version
        except Exception as exc:
            raise EncryptionError("invalid encryption envelope") from exc

    def rotate(self, value: str, context: str) -> str:
        if self.key_version(value) == self.active_version:
            return value
        return self.encrypt(self.decrypt(value, context), context)


def _rotate_value(cipher: EnvelopeCipher, value: str, context: str, allow_plaintext: bool) -> str:
    if value.startswith(PREFIX):
        return cipher.rotate(value, context)
    if not allow_plaintext:
        raise EncryptionError("plaintext persisted value requires explicit migration approval")
    return cipher.encrypt(value, context)


def rotate_sqlite(
    database: str, cipher: EnvelopeCipher, batch_size: int, allow_plaintext: bool,
) -> dict[str, int]:
    rotated = 0
    with sqlite3.connect(database, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "idempotency_records" in tables and rotated < batch_size:
            rows = db.execute(
                """SELECT tenant_id,key_hash,response_json FROM idempotency_records
                WHERE response_json IS NOT NULL AND response_json NOT LIKE ? LIMIT ?""",
                (f"{PREFIX}{cipher.active_version}:%", batch_size - rotated),
            ).fetchall()
            for row in rows:
                value = _rotate_value(
                    cipher, row["response_json"],
                    f"idempotency:{row['tenant_id']}:{row['key_hash']}", allow_plaintext,
                )
                if value != row["response_json"]:
                    db.execute(
                        "UPDATE idempotency_records SET response_json=? WHERE tenant_id=? AND key_hash=?",
                        (value, row["tenant_id"], row["key_hash"]),
                    )
                    rotated += 1
        if "exact_cache_entries" in tables and rotated < batch_size:
            rows = db.execute(
                """SELECT tenant_id,cache_key,response_json FROM exact_cache_entries
                WHERE response_json NOT LIKE ? LIMIT ?""",
                (f"{PREFIX}{cipher.active_version}:%", batch_size - rotated),
            ).fetchall()
            for row in rows:
                value = _rotate_value(
                    cipher, row["response_json"], f"cache:{row['tenant_id']}:{row['cache_key']}",
                    allow_plaintext,
                )
                if value != row["response_json"]:
                    db.execute(
                        "UPDATE exact_cache_entries SET response_json=? WHERE tenant_id=? AND cache_key=?",
                        (value, row["tenant_id"], row["cache_key"]),
                    )
                    rotated += 1
    return {"rotated": rotated}


async def rotate_postgres(pool, cipher: EnvelopeCipher, batch_size: int, allow_plaintext: bool):
    rotated = 0
    async with pool.acquire() as db, db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock(hashtext('llm-gateway-encryption-rotation'))")
        if await db.fetchval("SELECT to_regclass('idempotency_records')"):
            rows = await db.fetch("""SELECT tenant_id,key_hash,response_json FROM idempotency_records
                WHERE response_json IS NOT NULL AND response_json NOT LIKE $1 LIMIT $2 FOR UPDATE""",
                f"{PREFIX}{cipher.active_version}:%", batch_size)
            for row in rows:
                value = _rotate_value(cipher, row["response_json"],
                    f"idempotency:{row['tenant_id']}:{row['key_hash']}", allow_plaintext)
                if value != row["response_json"]:
                    await db.execute("""UPDATE idempotency_records SET response_json=$1
                        WHERE tenant_id=$2 AND key_hash=$3""", value, row["tenant_id"], row["key_hash"])
                    rotated += 1
        if rotated < batch_size and await db.fetchval("SELECT to_regclass('exact_cache_entries')"):
            rows = await db.fetch("""SELECT tenant_id,cache_key,response_json FROM exact_cache_entries
                WHERE response_json NOT LIKE $1 LIMIT $2 FOR UPDATE""",
                f"{PREFIX}{cipher.active_version}:%", batch_size - rotated)
            for row in rows:
                value = _rotate_value(cipher, row["response_json"],
                    f"cache:{row['tenant_id']}:{row['cache_key']}", allow_plaintext)
                if value != row["response_json"]:
                    await db.execute("""UPDATE exact_cache_entries SET response_json=$1
                        WHERE tenant_id=$2 AND cache_key=$3""", value, row["tenant_id"], row["cache_key"])
                    rotated += 1
    return {"rotated": rotated}

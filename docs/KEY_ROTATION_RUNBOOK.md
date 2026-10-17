# Phase 3 key-rotation runbook

## Envelope-encryption keys

Install `llm-gateway[security]`. Store `LLM_GATEWAY_ENCRYPTION_KEYS` in the deployment secret manager as a JSON object whose values are base64-encoded 32-byte keys, for example `{"v1":"<base64>"}`. Never put this value in YAML, logs, diagnostics, audit metadata, source control, or command history. Set `encryption.active_key_version` to a key present in the keyring and enable `features.envelope_encryption`.

Each persisted value has a random 256-bit data key. AES-256-GCM encrypts the value using record identity as associated data; the active versioned key-encryption key wraps the data key with separate nonce and associated context. Moving ciphertext between tenants, record types, or identifiers fails authentication.

### Rotation

1. Back up the database and verify the audit chain.
2. Add the new key version to the secret-manager value while retaining every old version still referenced by data.
3. Deploy the expanded keyring without changing `active_key_version`; verify readiness and reads.
4. Change `active_key_version` and rolling-restart all replicas. New writes now use the new version.
5. Repeatedly call `POST /admin/encryption/rotate`. Each bounded database transaction returns `rotated`; continue until it returns zero. A crash rolls back the current batch, and the next call safely resumes because envelopes carry their key version.
6. Exercise cache/idempotency reads, verify the audit chain, and retain old keys through the rollback window.
7. Only after database inspection and application verification show no old-version envelope may the old key be removed.

For pre-encryption rows, first stage the complete keyring, set `allow_plaintext_migration: true`, and run rotation to zero. Immediately return it to false. Without this explicit switch, rotation fails rather than silently accepting plaintext.

### Rollback and recovery

If reads fail after changing the active version, restore the previous active version while keeping both keys; envelopes written by either version remain readable. If rotation is interrupted, keep both keys and rerun it. If an old key was removed prematurely, restore it from the secret manager before retrying. A lost key cannot be reconstructed from an envelope; restore a backup only if it was encrypted under an available key.

## Provider credentials

Provider credential overlap and promotion are covered by `SEC-CREDROT-001`. Keep old and new credentials active only for the bounded validation window, promote only after a successful non-content-bearing provider probe, and revoke the old credential at the provider after all replicas report the new version active.

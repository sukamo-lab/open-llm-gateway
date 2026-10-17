# Phase 3 threat model

## Scope and trust boundaries

Phase 3 protects organization, project, principal, role, key, audit, encryption, credential, and residency control-plane state. The gateway process, configured database, deployment secret manager, TLS termination, OIDC issuer, provider accounts, and audit sink are separate trust boundaries. Provider region/residency labels are operator assertions unless verified by an external contract or attestation system.

The model assumes the host and gateway binary are trusted. A database superuser, host administrator, compromised identity provider, or compromised active key-encryption key can defeat controls within that boundary; mitigations reduce blast radius and provide external evidence but cannot make a compromised root trustworthy.

## Assets

- Provider and gateway credentials, OIDC bearer tokens, encryption keys, and plaintext data keys.
- Tenant prompts, provider responses, cached/idempotent responses, and customer identity mappings.
- Organization/project hierarchy, role assignments, scoped key metadata, and residency policy.
- Ordered audit evidence and export checkpoints.

## Threats and controls

### Cross-tenant access and privilege escalation

Hierarchy writes validate organization/project relationships at the storage boundary. RBAC is opt-in and deny-by-default: principal roles grant permissions, project scope narrows them, and immutable key ceilings can only reduce grants. Principal-bound key issuance rejects cross-organization and cross-project references. The deployment administrator remains a root control-plane identity and must be held in a separate secrets-manager policy.

Residual risks: delegated management and binding revocation are implemented, but hosted CI and live PostgreSQL verification remain pending while `IAM-RBAC-001` is partial. Expose admin endpoints only to the trusted operator network and repeat the authorization matrix against the production database topology before enablement.

### Token forgery, substitution, replay, and identity confusion

OIDC accepts only explicit asymmetric algorithms, requires `kid`, validates signature, exact issuer and audience, expiry, optional not-before, and subject, and binds mapped tenant/organization/project claims to stored active records. Discovery issuer mismatch, excessive JWKS, unknown key after refresh, malformed tokens, and network failure all fail closed. SAML is parsed only by an external broker.

Residual risks: live identity-provider interoperability and revocation-event integration are unverified. JWTs remain usable until expiry unless the principal is deactivated; choose short token lifetimes at the issuer.

### Key and sensitive-data disclosure

Gateway/provider keys are read from environment variables or mounted files and are never returned by status APIs. API keys are stored only as SHA-256 digests. Envelope encryption uses random data keys and AES-256-GCM with record identity as associated data; versioned KEKs wrap data keys. Disabled encryption imports no crypto module. Logs, metrics, audit metadata, configuration diagnostics, and feature ledger entries must never contain bearer tokens, keys, prompts, responses, or claims.

Residual risks: environment variables may be visible to host administrators; move KEKs to a future KMS/HSM adapter for stronger separation. Memory is plaintext while processing.

### Credential-rotation outage or confused fallback

Ordered credential overlap tries the primary reference first and moves to the next only on authentication/authorization failure. Credential identifiers and values are excluded from telemetry. Operators retain both provider credentials until every replica has loaded and validated the new ordering.

Residual risks: pre-byte streaming overlap is implemented, but provider validation/promotion APIs and live-provider rotation verification remain pending. Do not revoke the old credential until live rotation verification succeeds; a stream is never retried with another credential after any response byte is emitted.

### Audit deletion, alteration, injection, and exporter failure

Events contain bounded scalar metadata, exclude sensitive field names, are globally ordered, and chain canonical SHA-256 hashes. Database triggers reject ordinary update/delete statements. Export verifies from its prior hash, advances checkpoints only after sink success, retries at least once, and uses bounded batches for backpressure. An immutable external sink anchors evidence outside the database.

Residual risks: a database superuser can disable triggers and recompute an unanchored chain. Scheduled export and external anchoring remain operator responsibilities.

### Residency bypass

Enforcement pins requests to one named policy with non-empty residency requirements and `fallback: deny`. Request overrides cannot select another policy, and uncovered operations/models fail rather than use default routing.

Residual risks: labels are assertions and do not prove physical processing location. Provider subprocessors, logging, abuse monitoring, and backups require contractual review outside the gateway.

## Availability and failure policy

- OIDC, RBAC, residency enforcement, envelope authentication, audit-chain verification, and rotation validation fail closed.
- Audit export failure retains its checkpoint and does not delete events.
- Disabled Phase 3 modules create no endpoints or external clients and do not add request-path work.
- SQLite is single-process. PostgreSQL is required for shared hierarchy, roles, audit ordering, rotation, and horizontally scaled enforcement.

## Verification priorities

Before production enablement, run the full authorization matrix, cross-tenant adversarial tests, OIDC/JWKS rotation fixtures, audit corruption/export recovery tests, encryption interruption/rollback tests, provider credential overlap tests, residency fallback tests, and disabled/enabled latency benchmarks. Repeat against PostgreSQL and the selected real IdP, audit sink, KMS strategy, and providers without using customer credentials or content.

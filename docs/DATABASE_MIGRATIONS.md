# Database migrations

Database schemas are versioned independently by component. `usage` exists only when quotas or accounting are enabled; `identity` exists only when managed keys are enabled; `idempotency` exists only when persistent replay is enabled; `cache` exists only when the database cache backend is selected. Minimal single-user mode therefore creates no database or migration metadata.

## Inspect and upgrade

Status is read-only and has stable JSON output for automation:

```powershell
llm-gateway --config config.yaml --migration-status
llm-gateway --config config.yaml --migration-status --output json
```

Apply all pending migrations before deploying a new gateway version:

```powershell
llm-gateway --config config.yaml --migrate
```

The command exits `0` on success, `1` for a database/migration failure, and `2` for invalid configuration. Migrations are idempotent. Gateway startup also applies pending migrations for backward compatibility and simple single-node operation.

## Concurrency and availability

- SQLite uses an immediate transaction. Before a pending migration, it creates a consistent sibling backup named `gateway.db.pre-migration-<number>.bak`.
- PostgreSQL serializes migration runners using a transaction-scoped advisory lock. Schema history and each migration commit atomically.
- Multiple application replicas may start concurrently, but production deployments should run `--migrate` as a dedicated pre-deployment job before replacing application instances.
- A database schema newer than the running binary is rejected rather than guessed at.

## Upgrade procedure

1. Read the release notes and verify application/schema compatibility.
2. Back up the database. SQLite also creates its automatic pre-migration copy; PostgreSQL operators should use their managed backup or `pg_dump` process.
3. Run `--migration-status --output json` and archive its output with deployment records.
4. Run `--migrate` once with the new release artifact.
5. Start one gateway replica and verify `/ready`, diagnostics, authentication, and accounting.
6. Roll out remaining replicas and retain the backup until the rollback window closes.

## Rollback

Schema migrations are additive and do not provide destructive automatic down-migrations. This avoids silently discarding usage, key, or tenant data.

To roll back the application:

1. Stop all gateway writers.
2. If the previous release supports the current schema, deploy the previous binary without restoring data.
3. Otherwise restore the pre-upgrade SQLite copy or PostgreSQL backup to a new database location.
4. Point the previous release at the restored database and run `--migration-status` before admitting traffic.
5. Preserve the upgraded database separately until the incident is resolved.

Never copy an active SQLite database file directly; use the generated backup, the SQLite backup API, or stop all writers first.

### Usage schema version 2

Version 2 adds durable per-sink usage-export checkpoints. Existing accounting events remain unchanged and every new sink starts before the first completed event. Disable `finops_export` before restoring a pre-v2 backup; the sink must tolerate replay from its last externally anchored idempotency key.

### Usage schema version 3

Version 3 makes rows immutable after they reach `completed`. Database triggers reject later updates and deletes while preserving the required `reserved` to `completed` reconciliation transition. Startup retention cleanup continues to remove old failed or abandoned reservations, but finalized billing records remain available for deterministic replay and reconciliation. Restore a pre-v3 backup to roll back; do not disable the trigger on an active billing database.

### Identity schema version 2

Version 2 adds organizations, projects, principals, scoped indexes, and a nullable `tenants.organization_id` column. Existing tenants and hashed consumer keys are preserved and remain unassigned. Upgrade with the normal `--migrate` command before enabling `features.identity_hierarchy`.

Rollback uses the standard backup restore procedure above. Because the migration is additive, a previous binary may continue against the upgraded database only if it ignores unknown tables and columns; otherwise restore the pre-migration SQLite copy or PostgreSQL backup. Do not manually drop hierarchy tables while any newer gateway replica is running.

### Identity schema version 3

Version 3 adds role bindings plus nullable principal, project, and permission-ceiling metadata to consumer keys. Existing keys remain valid while RBAC is disabled and are intentionally denied after RBAC is enabled. Roll back by disabling RBAC first, stopping newer replicas, and restoring the pre-upgrade backup when the previous binary cannot tolerate the additive schema.

### Identity schema version 4

Version 4 adds the nullable indexed `tenants.plan_id` assignment used by opt-in versioned FinOps plans. Existing tenants remain unassigned and preserve their explicit quota fields. Before rollback, disable `finops_plans`, stop newer replicas, and restore the automatic pre-migration SQLite backup or the corresponding PostgreSQL backup if the previous binary cannot tolerate the additive column.

### Identity schema version 5

Version 5 adds append-only tenant credit grants with optional expiration and an active-credit lookup index. Existing plan balances are unchanged. Credit rows cannot be updated or deleted; corrections must be represented by operationally reviewed compensating grants in a later schema/API revision. Disable `finops_plans`, stop writers, and restore the pre-migration backup to roll back.

### Audit schema version 1

Audit version 1 creates append-only events, mutation-prevention triggers, and exporter checkpoints. Rollback requires disabling `audit_ledger`, stopping all newer replicas, and restoring the pre-upgrade backup. Do not drop or truncate audit tables as a down-migration; retain or export them according to policy.

### Jobs schema version 1

Jobs version 1 creates durable job state, tenant/creation and state/lease indexes, cancellation state, and lease metadata. Disable `async_jobs` and stop API/worker replicas before restoring the pre-migration backup. Queued work created after that backup must be reconciled before rollback.

### Jobs schema version 2

Jobs version 2 adds the signed-webhook outbox, delivery leases, retry schedule, attempt counter, and bounded error code. A terminal result and its completion event are committed atomically. Delivery workers reclaim expired leases after process termination and sinks must deduplicate the stable job-ID idempotency key. Before rollback, stop job and delivery workers and reconcile every non-delivered outbox row.

### Coordination schema version 1

Coordination version 1 creates shared provider circuit rows and expiring concurrency leases. Disable `distributed_resilience` and restart every replica before rollback so all processes return to local circuit and semaphore state.

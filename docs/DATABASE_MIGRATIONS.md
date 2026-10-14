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

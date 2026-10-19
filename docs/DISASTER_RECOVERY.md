# Backup, restore, and disaster recovery

Define deployment-specific RPO and RTO before production. The example targets are a 15-minute RPO and 60-minute RTO; they are not guarantees.

## SQLite

SQLite supports one gateway process. Create a consistent online backup and SHA-256 manifest with:

```powershell
python scripts/sqlite_backup.py create data/gateway.db backups/gateway-2026-10-18.db
python scripts/sqlite_backup.py verify backups/gateway-2026-10-18.db
```

Restore only while every gateway writer is stopped. The command refuses to overwrite an existing destination:

```powershell
python scripts/sqlite_backup.py restore backups/gateway-2026-10-18.db data/restored.db --confirm
```

Run configuration diagnostics and migration status against the restored copy before switching traffic.

## PostgreSQL

Use the platform's encrypted physical backups plus point-in-time recovery, or `pg_dump --format=custom` for a logical recovery path. Store credentials outside command lines. Regularly restore into an isolated database, run `llm-gateway --migration-status --output json`, audit-chain verification, tenant-boundary smoke tests, usage reconciliation, and queued-job inspection.

## Multi-replica upgrade and failover drill

1. Stop submissions that cannot be safely retried and record the usage-export/audit checkpoints.
2. Run and verify a backup; restore it into an isolated target before the maintenance window.
3. Apply migrations once, then roll one gateway replica as a canary.
4. Verify readiness, authentication, tenant isolation, routing, job leases, circuits, and export checkpoints.
5. Roll remaining replicas while respecting the disruption budget.
6. Terminate one API and one worker during live test work; confirm leases expire and another worker recovers jobs.
7. Simulate primary-database loss using the platform failover mechanism and measure achieved RPO/RTO.
8. Reconcile usage/audit exports and document every gap. Roll back by stopping new replicas and restoring the tested backup when schema compatibility does not permit binary rollback.

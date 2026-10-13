# Runtime modes and feature flags

`mode` selects ergonomic defaults; every major subsystem can then be overridden under `features`.

## Single-user mode

```yaml
mode: single
features:
  authentication: false
  admin_api: false
  managed_keys: false
  tenant_quotas: false
  provider_quotas: false
  usage_accounting: false
  cost_tracking: false
  metrics: false
  circuit_breaker: true
  structured_logging: false
```

Single-user defaults require only a provider key, bind to localhost, do not initialize tenant/key infrastructure, and select the no-database `NullQuotaStore` hot path. Provider quotas or accounting can be enabled independently when desired.

Create this profile with:

```powershell
llm-gateway --init --init-mode single
```

## Multi-user mode

```yaml
mode: multi
features:
  authentication: true
  admin_api: true
  managed_keys: true
  tenant_quotas: true
  provider_quotas: true
  usage_accounting: true
  cost_tracking: true
  metrics: true
  circuit_breaker: true
  structured_logging: true
```

Multi-user defaults enable tenant identities, expiring consumer keys, tenant/provider limits, accounting, costs, metrics, and circuit breaking. PostgreSQL is recommended for multiple replicas.

```powershell
llm-gateway --init --init-mode multi
```

## Feature availability

Disabled features are not merely hidden:

- `admin_api: false` removes every `/admin/*` route.
- `managed_keys: false` avoids identity-store initialization and removes tenant/key routes.
- `metrics: false` removes `/metrics` and uses a no-op metrics object.
- disabling `provider_quotas`, `tenant_quotas`, and `usage_accounting` selects a no-I/O quota implementation.
- `tenant_quotas: false` removes tenant-limit checks from the request path.
- `cost_tracking: false` avoids price calculations and cost persistence.
- `circuit_breaker: false` avoids circuit state bookkeeping.
- `structured_logging: false` removes request correlation and provider log calls from the request path; normal server console logging remains available.
- `authentication: false` removes consumer authentication checks, but is accepted only on a localhost bind.

Invalid dependency combinations fail configuration validation: managed keys require authentication and the admin API; tenant quotas require managed keys; cost tracking requires accounting.

## Latency

The gateway creates one shared upstream HTTP connection pool and reuses TLS connections. Single-user minimal mode performs no identity lookup, metrics update, circuit mutation, or quota database transaction. Multi-user authentication uses a bounded TTL cache; PostgreSQL quota reservations remain transactional across replicas. Use `scripts/load_test.py` to measure local overhead and full provider latency independently.

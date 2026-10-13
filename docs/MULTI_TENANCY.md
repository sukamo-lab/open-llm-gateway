# Multi-tenancy and consumer keys

Enable `auth.enabled` and set `LLM_GATEWAY_ADMIN_KEY`. Bootstrap keys in `LLM_GATEWAY_API_KEYS` are useful for migration; managed consumer keys are recommended for applications.

## Create a tenant

The operator CLI loads `.env` automatically:

```powershell
llm-gateway-admin tenant-create acme-production `
  --requests-per-minute 120 --tokens-per-day 2000000 --cost-per-30-days 100
llm-gateway-admin tenant-list
```

The equivalent raw API call is:

```powershell
$admin = @{ Authorization = "Bearer $env:LLM_GATEWAY_ADMIN_KEY" }
$tenant = Invoke-RestMethod http://localhost:8000/admin/tenants `
  -Method Post -Headers $admin -ContentType application/json `
  -Body (@{
    name = "acme-production"
    requests_per_minute = 120
    tokens_per_day = 2000000
    cost_per_30_days = 100
  } | ConvertTo-Json)
```

## Issue a consumer key

```powershell
llm-gateway-admin key-issue TENANT_ID backend-service --expires-in-days 90
llm-gateway-admin key-list TENANT_ID
llm-gateway-admin key-revoke KEY_ID --yes
```

The equivalent raw API call is:

```powershell
$issued = Invoke-RestMethod "http://localhost:8000/admin/tenants/$($tenant.id)/keys" `
  -Method Post -Headers $admin -ContentType application/json `
  -Body (@{ name = "backend-service" } | ConvertTo-Json)
$issued.api_key
```

The plaintext key is returned once. Store it in your secrets manager. The gateway stores only its SHA-256 digest. Keys may have an expiration date. List key metadata with `GET /admin/tenants/{tenant_id}/keys`, suspend a tenant with `PATCH /admin/tenants/{tenant_id}`, and revoke with `DELETE /admin/keys/{key_id}`.

Consumers send their gateway key as `Authorization: Bearer lgw_live_...`. They never receive or need upstream provider credentials.

## Isolation and limits

Every reservation records `consumer_id`. Tenant request/minute, token/day, and rolling 30-day USD limits are checked atomically alongside provider quotas before an upstream call. Failed upstream attempts release the reservation. Administrators can inspect aggregate tenant usage at `GET /admin/usage/tenants`.

## Horizontal scaling

Set `database` to a PostgreSQL DSN for multiple gateway replicas. PostgreSQL uses pooled async connections and transaction-scoped advisory locks, preventing two replicas from overspending the same provider or tenant quota. SQLite is retained for desktop, development, and one-instance deployments.

Consumer authentication has a configurable TTL cache. Set `key_cache_seconds: 0` for immediate revocation across replicas; a value around 5 seconds minimizes database latency while bounding revocation delay. Provider calls normally dominate latency by hundreds or thousands of milliseconds; cached key validation and quota transactions are intended to remain in the low-millisecond range.

Run the PostgreSQL Compose topology with:

```powershell
$env:POSTGRES_PASSWORD = "use-a-long-random-password"
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build -d
```

The supplied Compose topology is a convenient single-gateway PostgreSQL deployment. For horizontal replicas, remove the fixed host-port publication, deploy the same image/configuration through your orchestrator, and place a load balancer in front. No sticky sessions are required. Streaming connections require load-balancer timeouts suitable for SSE.

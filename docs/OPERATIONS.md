# Operations

## Endpoints

- `GET /health`: process liveness.
- `GET /ready`: at least one enabled provider has credentials.
- `GET /v1/models`: OpenAI-compatible model aliases.
- `POST /v1/chat/completions`: OpenAI-compatible chat, including SSE streaming.
- `GET /admin/providers`: configuration, circuit state, failures, and retry time; secrets are never returned.
- `GET /admin/usage`: persistent request/token totals.
- `GET /admin/usage/tenants`: tenant-attributed requests, tokens, and spend.
- `/admin/tenants` and `/admin/keys/*`: tenant and consumer-key lifecycle.
- `GET /metrics`: Prometheus text format.
- `/docs`: interactive API documentation when enabled.

Every response includes `X-Request-ID`. Supply your own header to correlate gateway and application logs.

## Preflight

`llm-gateway --check` performs strict YAML/schema/secret-source validation. `llm-gateway --doctor` additionally reports unsafe deployment choices and verifies SQLite/PostgreSQL connectivity plus schema initialization. It exits nonzero on errors, making it suitable for CI and container pre-deployment checks. `llm-gateway --generate-secret` prints a cryptographically random secret.

`llm-gateway --init --init-mode single` creates the no-database minimal profile. `--init-mode multi` creates secured consumer/admin credentials and enables the managed feature set.

`llm-gateway --migration-status --output json` reports component schema versions without changing them. `llm-gateway --migrate` applies pending migrations and is suitable for a deployment pre-start job. See [Database migrations](DATABASE_MIGRATIONS.md).

## Production checklist

1. Copy `config.example.yaml` and `.env.example`; keep both real files out of source control.
2. Enable authentication and generate separate client/admin secrets.
3. Configure exact vendor quotas and pricing from each account dashboard.
4. Disable provider override and restrict CORS.
5. Persist `/app/data`, terminate TLS at a reverse proxy/load balancer, and scrape `/metrics` using the admin credential.
6. Back up the SQLite database. For multi-replica scale-out, migrate accounting to a shared transactional store before adding replicas.
7. Pin image versions, scan dependencies, and rotate provider credentials regularly.

An unauthenticated gateway is permitted only on a localhost bind. Readiness fails when authentication is disabled and the configured host exposes the service to the network.

## Circuit breaker

Transport errors, rate limits, invalid credentials, removed models, and malformed upstream responses count as failures. At the configured threshold, that provider is skipped during its cooldown. A later successful request closes the circuit. Local configured quotas skip a provider without counting as an upstream failure.

## Load and latency testing

Measure local gateway overhead separately from provider latency:

```powershell
python scripts/load_test.py --requests 1000 --concurrency 50
```

Then measure the full authenticated path with a disposable consumer key:

```powershell
$env:LLM_GATEWAY_BENCHMARK_KEY = "lgw_live_..."
python scripts/load_test.py --chat --requests 100 --concurrency 10 --model fast
```

The tool reports failures, throughput, mean, p50, p95, and p99. Run it from the same network as consumers and establish service-level objectives before production rollout.

## Release verification

Cross-platform unit, package, PostgreSQL integration, and container build checks are defined in `.github/workflows/ci.yml`. See [Development and verification](DEVELOPMENT.md) for equivalent local commands. Production releases should be made only from a revision where every required job passes.

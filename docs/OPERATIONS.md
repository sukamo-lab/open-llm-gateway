# Operations

## Endpoints

- `GET /health`: process liveness.
- `GET /ready`: at least one enabled provider has credentials.
- `GET /v1/models`: OpenAI-compatible model aliases.
- `POST /v1/chat/completions`: OpenAI-compatible chat, including SSE streaming.
- `POST /v1/responses`: optional Responses compatibility API for non-streaming text, vision, reasoning, JSON, and function-tool requests.
- `POST /v1/embeddings`: optional OpenAI-compatible embeddings API with capability-aware routing and failover.
- `POST /v1/moderations`: optional OpenAI-compatible deterministic text classification without an upstream provider call.
- `POST /v1/audio/speech`: optional OpenAI-compatible text-to-speech with capability-aware routing and failover.
- `POST /v1/images/generations`: optional OpenAI-compatible image generation returning URL or base64 data.
- `GET /admin/providers`: configuration, circuit state, failures, and retry time; secrets are never returned.
- `POST /admin/model-discovery`: optional read-only upstream catalog drift report.
- `GET /admin/usage`: persistent request/token totals.
- `GET /admin/usage/tenants`: tenant-attributed requests, tokens, and spend.
- `/admin/tenants` and `/admin/keys/*`: tenant and consumer-key lifecycle.
- `GET /metrics`: Prometheus text format.
- `/docs`: interactive API documentation when enabled.

Every response includes `X-Request-ID`. Supply your own header to correlate gateway and application logs.

With `features.structured_logging`, each request emits one JSON completion event carrying the same `request.id`; authenticated traffic also includes the non-secret tenant identifier. Provider retry events carry provider/model/attempt metadata but never prompts, responses, headers, or credentials. Send stdout/stderr to the platform log collector rather than writing rotating files inside the container.

## Preflight

`llm-gateway --check` performs strict YAML/schema/secret-source validation. `llm-gateway --doctor` additionally reports unsafe deployment choices and verifies SQLite/PostgreSQL connectivity plus schema initialization. It exits nonzero on errors, making it suitable for CI and container pre-deployment checks. `llm-gateway --generate-secret` prints a cryptographically random secret.

Model discovery is operator-triggered and read-only. Protect it with the admin API credential in authenticated deployments. Review `unconfigured` models before adding explicit aliases and capabilities to configuration; discovery never activates a model or changes routing. An upstream failure is isolated to that provider's report so other catalogs remain visible.

For least-cost routing, keep pricing metadata under change control and alert on stale `pricing_version` values through your configuration-management process. Use response `gateway.routing` metadata to explain selections. `unknown_price_policy: exclude` can make every otherwise eligible model unavailable, while `last` preserves availability at the risk of selecting a model whose cost is unknown.

For latency routing, begin with a conservative cold-start estimate and exploration interval, then inspect `/admin/providers` observations and existing provider-duration histograms. EWMA state resets on process restart and differs across workers/replicas; use one worker when consistent selection is required. Setting exploration to `0` disables sampling but can leave a temporarily slow candidate permanently disadvantaged.

`llm-gateway --init --init-mode single` creates the no-database minimal profile. `--init-mode multi` creates secured consumer/admin credentials and enables the managed feature set.

`llm-gateway --migration-status --output json` reports component schema versions without changing them. `llm-gateway --migrate` applies pending migrations and is suitable for a deployment pre-start job. See [Database migrations](DATABASE_MIGRATIONS.md).

When idempotency is enabled, monitor database growth and choose a TTL appropriate for client retry windows. PostgreSQL coordinates claims across replicas. SQLite is suitable only for a single gateway process. Treat HTTP 409 with `Retry-After` as an in-progress duplicate and retry later using the same key and unchanged body.

For exact caching, use the memory backend only for a single process. Use PostgreSQL or Redis for multiple replicas, monitor hit ratio and retained bytes at the backend, and increment `cache.namespace` for an immediate logical invalidation. Provider/model configuration changes invalidate keys automatically. Redis must enforce its own authentication, TLS, network policy, memory limit, and eviction policy.

For tracing, send OTLP/HTTP to a local or regional OpenTelemetry Collector rather than directly coupling the gateway to a monitoring vendor. Protect collector ingestion with TLS and authentication, bound exporter queues, and select a sampling ratio appropriate for traffic. Export occurs in batches off the request path; shutdown attempts to flush queued spans. Keep tenant IDs disabled unless the monitoring destination is approved for that metadata.

For SLA monitoring, import `deploy/grafana/llm-gateway-dashboard.json` and load `deploy/prometheus/llm-gateway-alerts.yml` into Prometheus. Review the example 90% provider success, 30-second p95, 5% gateway 5xx, traffic-volume, and duration thresholds against your own objectives before enabling notifications. Alert on validated `provider_results` for user-visible reliability and use raw `provider_requests` plus retry metrics for upstream troubleshooting.

For PII policy operation, monitor `llm_gateway_pii_events_total` by stage, action, and outcome. Investigate scanner errors immediately in fail-open deployments because requests may have continued without evaluation. Logs contain detector types and counts but not matched values. Before enabling blocking or redaction, run representative and adversarial test data through a non-production environment and document acceptable false-positive behavior. Output scanning with SSE must either block streaming or explicitly report a bypass.

For moderation, monitor `llm_gateway_moderation_events_total` by stage, action, and outcome. Treat error or bypass outcomes as policy-control degradation. Begin with `action: monitor`, measure category-level false positives and missed cases using an approved corpus, and only then move selected deployments to blocking. The built-in rules are a deterministic baseline, not a substitute for a separately evaluated moderation service or model where the risk profile requires one.

The Moderations API records `stage="api"` and `action="classify"` once per classified input, so batch requests may increment the counter multiple times. Its scores indicate deterministic rule matches rather than probability or severity. It consumes no provider quota. Keep request bounds appropriate for synchronous CPU work and validate the baseline against an approved corpus before relying on it for safety decisions.

For prompt-injection policy, monitor `llm_gateway_prompt_injection_events_total` by action and outcome. Begin with scoring, compare flagged and clean traffic against an approved adversarial corpus, and calibrate the threshold before warning, blocking, or routing. Treat fail-open errors as control degradation. Review every `prompt_injection_safe` model designation together with tool permissions, data access, sandboxing, and provider retention policy; the designation does not make a model inherently injection-proof.

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

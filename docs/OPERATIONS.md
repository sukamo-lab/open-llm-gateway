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
- `GET /admin/usage/export`: optional cursor-paginated completed usage with a canonical page checksum.
- `GET /admin/usage/reconcile`: optional bounded accounting totals for reconciliation.
- `POST /admin/usage/export-deliver`: checkpointed at-least-once delivery to the configured HTTPS sink.
- `GET /v1/jobs/{job_id}` and `DELETE /v1/jobs/{job_id}`: optional tenant-isolated durable job status and cancellation.
- `/admin/tenants` and `/admin/keys/*`: tenant and consumer-key lifecycle.
- `GET /admin/plans`: optional configured versioned plan catalog.
- `GET /admin/tenants/{tenant_id}/budget`: usage-backed credit and budget-threshold status.
- `GET /metrics`: Prometheus text format.
- `/docs`: interactive API documentation when enabled.

Every response includes `X-Request-ID`. Supply your own header to correlate gateway and application logs.

With `features.structured_logging`, each request emits one JSON completion event carrying the same `request.id`; authenticated traffic also includes the non-secret tenant identifier. Provider retry events carry provider/model/attempt metadata but never prompts, responses, headers, or credentials. Send stdout/stderr to the platform log collector rather than writing rotating files inside the container.

## Preflight

`llm-gateway --check` performs strict YAML/schema/secret-source validation. `llm-gateway --doctor` additionally reports unsafe deployment choices and verifies SQLite/PostgreSQL connectivity plus schema initialization. It exits nonzero on errors, making it suitable for CI and container pre-deployment checks. `llm-gateway --generate-secret` prints a cryptographically random secret.

Model discovery is operator-triggered and read-only. Protect it with the admin API credential in authenticated deployments. Review `unconfigured` models before adding explicit aliases and capabilities to configuration; discovery never activates a model or changes routing. An upstream failure is isolated to that provider's report so other catalogs remain visible.

For least-cost routing, keep pricing metadata under change control and alert on stale `pricing_version` values through your configuration-management process. Use response `gateway.routing` metadata to explain selections. `unknown_price_policy: exclude` can make every otherwise eligible model unavailable, while `last` preserves availability at the risk of selecting a model whose cost is unknown.

For latency routing, begin with a conservative cold-start estimate and exploration interval, then inspect `/admin/providers` observations and existing provider-duration histograms. EWMA state resets on process restart and differs across workers/replicas; use one worker when consistent selection is required. Setting exploration to `0` disables sampling but can leave a temporarily slow candidate permanently disadvantaged.

For declarative routing policies, treat region, residency, and quality metadata as controlled operator assertions. Prefer `fallback: deny` for residency-sensitive traffic; `default` explicitly relaxes the rule when no constrained candidate is available. Keep client policy override disabled unless callers are separately authorized to choose among configured policies.

When configuration reload is enabled, validate candidate files with `llm-gateway --check` before calling `POST /admin/config/reload`. A successful response applies only routing and provider/model changes to that process. Rejected files preserve the last-known-good router. Reload every replica and compare the returned generation, or use a rolling restart. Restart before `max_generations` is exhausted and whenever changing features, authentication, storage, middleware, or exact-cache configuration.

`llm-gateway --init --init-mode single` creates the no-database minimal profile. `--init-mode multi` creates secured consumer/admin credentials and enables the managed feature set.

For versioned plans, treat each `pricing_version` as immutable configuration under change control. Assign only plan IDs returned by `GET /admin/plans`; unknown IDs are rejected by tenant administration. Plan quota ceilings compose with tenant overrides, and an existing tenant assignment survives restart in SQLite or PostgreSQL. Back up and migrate the identity component before enabling this feature. Poll the budget endpoint to deliver alerts through your existing automation; its triggered-threshold list is deterministic and can be deduplicated by tenant, pricing version, and threshold.

For usage export, persist both fields from the returned `next` cursor and verify each page's SHA-256 over its sorted compact JSON event array. Pages contain only completed records and are ordered by creation time plus request ID. Reconciliation is intentionally bounded at 10,000 events; `complete: false` means the operator must reconcile exported pages externally rather than treating the totals as global.

The delivery endpoint sends one configured bounded page with its checksum as `Idempotency-Key`. The sink must deduplicate on that key. A timeout, non-2xx response, or process interruption before checkpointing replays the same page; the checkpoint advances only after a 2xx response. Invoke until `exported` is zero.

`llm-gateway --migration-status --output json` reports component schema versions without changing them. `llm-gateway --migrate` applies pending migrations and is suitable for a deployment pre-start job. See [Database migrations](DATABASE_MIGRATIONS.md).

When idempotency is enabled, monitor database growth and choose a TTL appropriate for client retry windows. PostgreSQL coordinates claims across replicas. SQLite is suitable only for a single gateway process. Treat HTTP 409 with `Retry-After` as an in-progress duplicate and retry later using the same key and unchanged body.

For exact caching, use the memory backend only for a single process. Use PostgreSQL or Redis for multiple replicas, monitor hit ratio and retained bytes at the backend, and increment `cache.namespace` for an immediate logical invalidation. Provider/model configuration changes invalidate keys automatically. Redis must enforce its own authentication, TLS, network policy, memory limit, and eviction policy.

For semantic caching, begin with a high similarity threshold and a new `embedding_version` for every vectorizer change. The current memory backend is replica-local; do not expect cross-worker hits. Treat cached prompt and response material as customer content, keep tenant partitioning enabled, and prefer PII redaction instead of `allow_sensitive_content`.

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

## Audit ledger and export

Enable `features.audit_ledger` only after choosing the shared database appropriate for the deployment. SQLite serializes a single process; PostgreSQL uses an advisory lock so replicas append to one global chain. Verify it with `GET /admin/audit/verify` and retrieve bounded records with `GET /admin/audit/events?after=<sequence>&limit=<count>`.

Configure `audit.export_url`, optional `audit.export_token_env`, and a bounded batch size, then call `POST /admin/audit/export`. Delivery is at-least-once: the sink must deduplicate by `sequence` plus `event_hash` or the `Idempotency-Key` header. The checkpoint advances only after a 2xx response. A timeout, non-2xx response, or process interruption before checkpointing replays the batch; an interruption after checkpointing starts at the next event. Repeatedly invoke export until `exported` is zero. Apply upstream rate limits and monitor 502 responses; the batch bound provides backpressure and the gateway never drops records to catch up.

Exported records can be independently verified by starting with 64 zeroes as `previous_hash`, checking strict sequence ordering, canonicalizing the documented event fields as sorted compact JSON, and comparing each SHA-256 digest with `event_hash`. Database triggers prevent ordinary updates/deletes, but a database superuser can disable them and recompute the chain. Send exports to immutable external storage or a transparency service to anchor trust outside the gateway database.

Run `python scripts/verify_audit_export.py export.json` outside the gateway process to verify a complete JSON batch, `{ "events": [...] }` payload, or JSON Lines file. For a later segment, pass the previously anchored `--checkpoint-sequence` and `--checkpoint-hash`.

## Plan credits and budgets

Credit grants are append-only financial records. Grant them only through `POST /admin/tenants/{tenant_id}/credits`, use a stable non-secret source reference, and set `expires_in_days` for time-limited promotions. Poll `GET /admin/tenants/{tenant_id}/budget` for the active purchased total, remaining combined credit, spend percentage, and crossed thresholds. Database triggers reject edits and deletes; investigate an incorrect grant through the audit trail rather than modifying it in place.

## Durable batches and webhooks

Batch prompts and results are sensitive durable records. Enable payload persistence only on an encrypted, access-controlled database with a documented retention process. Monitor queued/running job age and webhook outbox attempts. Worker and notifier leases are safe to reclaim after their configured expiry; do not manually rewrite running ownership. Completion delivery is at-least-once, so sinks must deduplicate the job ID from `Idempotency-Key`, verify the timestamped HMAC signature, and reject stale timestamps according to local policy.

## Residency enforcement

Treat configured provider regions and residency labels as operator assertions unless your provider contract and deployment process independently attest them. With `residency_enforcement` enabled, readiness/configuration validation requires a named policy with required residency labels and denied fallback. Requests cannot select a different policy, and an operation or model outside the enforced policy is rejected rather than routed without residency constraints. During an outage, add only a provider that satisfies the same policy; do not change fallback to `default` as an availability workaround.

An unauthenticated gateway is permitted only on a localhost bind. Readiness fails when authentication is disabled and the configured host exposes the service to the network.

## Circuit breaker

Transport errors, rate limits, invalid credentials, removed models, and malformed upstream responses count as failures. At the configured threshold, that provider is skipped during its cooldown. A later successful request closes the circuit. Local configured quotas skip a provider without counting as an upstream failure.

With `distributed_resilience` enabled, PostgreSQL becomes the coordination authority for circuits and concurrency. Live requests renew their slot lease every third of `coordination.lease_seconds`; choose a value long enough to tolerate brief database interruptions without premature expiry. Worker or process termination needs no manual cleanup because renewal stops and expired leases are removed during subsequent acquisition. Monitor database latency because coordination adds shared-store operations to enabled request attempts.

## Retry and streaming recovery

Same-provider retries honor the configured attempt limit, exponential backoff, jitter, `Retry-After`, and per-provider request deadline for both ordinary and streaming chat requests. A stream can retry or fail over only before its first byte reaches the client. If an upstream connection fails after SSE output starts, the gateway terminates the client stream and records the failed attempt; it never appends output from another attempt or provider. Clients should treat an interrupted stream as incomplete and decide whether the whole request is safe to submit again.

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

## Backup and disaster recovery

Use `scripts/sqlite_backup.py` for online SQLite backup plus manifest verification and guarded restore. PostgreSQL deployments must use encrypted platform backups/PITR and execute the multi-replica recovery drill in [Disaster recovery](DISASTER_RECOVERY.md). Record achieved RPO/RTO rather than treating example targets as guarantees.

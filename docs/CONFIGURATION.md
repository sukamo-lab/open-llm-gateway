# Configuration reference

Configuration is YAML and supports `${ENVIRONMENT_VARIABLE}` plus `${VARIABLE:-default}` substitution. Provider secrets should use `api_key_env` or `api_key_file`; `api_key_value` exists for local development only. Authentication similarly supports `api_keys_file` and `admin_key_file` for mounted Docker/Kubernetes secrets.

`mode: single` selects minimal local defaults; `mode: multi` selects the managed product defaults. The `features` section overrides individual subsystems. See [Runtime modes and feature flags](MODES_AND_FEATURES.md).

Large configurations can use `include: [providers/*.yaml]`. Paths and glob patterns are resolved relative to the including file. Included dictionaries are deep-merged, lists are appended, and the including file takes precedence for scalar values. Include cycles and unmatched patterns are rejected.

## Server

- `host`, `port`: bind address and port. CLI flags override them.
- `docs_enabled`: serves interactive OpenAPI documentation at `/docs`.
- `cors_origins`: explicit browser origins. Keep empty for server-to-server deployments.

## Authentication

When `auth.enabled` is true, `/v1/*` requires a bearer token listed in the comma-separated environment variable named by `api_keys_env`. `/admin/*` and `/metrics` require the separate key named by `admin_key_env`. `/health` and `/ready` intentionally remain unauthenticated for orchestrator probes.

`managed_keys_enabled` allows administrator-issued tenant keys. `key_cache_seconds` trades immediate cross-replica revocation for fewer identity lookups; use `0` for immediate enforcement or a small value such as `5` for production.

## Routing

- `max_attempts`: maximum upstreams attempted; `0` means every eligible candidate.
- `paid_fallback`: set false to prohibit paid providers globally.
- `allow_provider_override`: enables the non-standard request field `provider`; useful for diagnostics but normally false on public deployments.
- `http_max_connections`, `http_max_keepalive_connections`, and `http_keepalive_expiry_seconds` tune the shared upstream connection pool. Reused TLS connections minimize gateway overhead.
- `retry_attempts` is the total number of attempts for one provider candidate; `1` disables same-provider retries and preserves immediate failover. Transient transport errors and HTTP 408/425/429/5xx responses use bounded exponential backoff and jitter. `Retry-After` is honored up to `retry_max_backoff_seconds`.
- `request_deadline_seconds` optionally bounds all non-streaming attempts against one provider. Keep it below the consumer/load-balancer timeout. Streaming retries currently occur through provider failover only before a successful stream is established.
- Provider `tier` is `free` or `paid`. Free always sorts first.
- Lower model `priority` sorts first within a tier.
- `failure_threshold` consecutive errors open that provider's circuit for `cooldown_seconds`.
- `max_concurrency` prevents one upstream account from being overloaded.

## Quotas and models

Each provider may have multiple rolling `limits`. A limit can constrain `requests`, `tokens`, `cost_usd`, or any combination over `window_seconds`. Multiple windows can be combined. Reservations are atomic and persisted in SQLite; dollar limits reserve a conservative worst-case estimate and reconcile against reported input/output usage.

Model aliases decouple clients from vendor model IDs. Capabilities document support for `chat`, `reasoning`, `vision`, `tools`, and `json`. A request with `reasoning_effort` only considers reasoning models.

OpenAI-compatible variants can customize `chat_completions_path`, `api_key_header`, `api_key_prefix`, provider `request_defaults`, model `request_overrides`, and arbitrary headers without adding a new adapter. Client request values take precedence over configured defaults and overrides.

## Structured logging

`features.structured_logging` enables newline-delimited JSON logs. It defaults off in single-user mode and on in multi-user mode. When disabled, request correlation and provider log calls are absent from the hot path.

Logs use stable dotted fields including `@timestamp`, `service.name`, `service.version`, `log.level`, `request.id`, `tenant.id`, `http.*`, and provider/model attempt metadata. Request bodies, response bodies, authorization headers, consumer keys, and provider credentials are never logged. Health/readiness request logs are suppressed unless `observability.log_health_checks` is enabled.

`observability.include_exception_stacktraces` is false by default because library or provider exceptions may contain operational details. Enable it only where the log destination has appropriate access controls and retention.

## Idempotency

`features.idempotency` enables persistent handling of the `Idempotency-Key` request header for non-streaming chat completions. It defaults off in single-user mode and on in multi-user mode. Keys are SHA-256 hashed before persistence, scoped to the authenticated tenant, and never logged.

The first request owns a time-limited lease. Concurrent replicas coalesce on the same database record; completed responses replay with `Idempotent-Replayed: true`. Reusing a key with a different canonical request returns HTTP 409. Failed upstream requests release their claim so a later call can retry. Expired leases allow recovery when an owner process terminates.

- `ttl_seconds`: replay retention, from one minute to seven days.
- `lease_seconds`: maximum ownership period before another replica may recover an abandoned request. Set it above the maximum upstream deadline.
- `wait_timeout_seconds` and `poll_interval_seconds`: bound concurrent duplicate waiting.
- `max_response_bytes`: responses larger than this are returned but not persisted; inspect `Idempotency-Stored`.
- `min_key_length` and `max_key_length`: accepted visible-ASCII header length.

Streaming requests reject `Idempotency-Key`: replaying a partially delivered SSE response could duplicate side effects or corrupt client state.

Cached records contain model responses and should receive the same database access controls, encryption, backup policy, and retention review as other application data. Disable the feature for workloads whose responses must not be persisted.

## Exact response cache

`features.exact_cache` enables tenant-isolated replay of successful, non-streaming chat responses. It is opt-in in both modes. Canonical SHA-256 cache keys include the full request, a non-secret routing-configuration signature, and `cache.namespace`; prompts are never stored in cache keys. Responses are retained, so apply appropriate encryption, access, backup, and retention controls.

Backends:

- `memory`: bounded LRU cache for one process; fastest and suitable for local use.
- `database`: SQLite for one process or PostgreSQL for shared, durable replica coordination.
- `redis`: shared low-latency cache using the URL from `redis_url_env`; Redis eviction and memory limits remain operator responsibilities.

`ttl_seconds`, `max_entries`, and `max_response_bytes` bound retention. `namespace` provides an operator-controlled invalidation generation. Routing/model configuration changes automatically change the key signature. Successful misses return `X-Cache: MISS`; hits return `X-Cache: HIT` and `Age`; streams, oversized responses, and client bypasses return `X-Cache: BYPASS`.

`failure_mode: open` preserves gateway availability when cache startup, reads, or writes fail and marks requests as bypassed; errors remain in operator logs. `closed` rejects requests instead and is appropriate only when policy requires guaranteed cache behavior.

## OpenTelemetry tracing

`features.tracing` enables W3C Trace Context extraction, content-safe server spans, provider-attempt client spans, and OTLP/HTTP batch export. It is opt-in in both modes. Disabled tracing does not import or initialize the telemetry SDK.

Set `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to the collector traces endpoint, normally `https://collector.example/v1/traces`. Standard OpenTelemetry variables such as `OTEL_EXPORTER_OTLP_HEADERS` and certificate settings configure authentication and TLS without placing secrets in YAML. A vendor-neutral Collector example is provided at `deploy/otel-collector.example.yaml`.

- `sample_ratio`: parent-aware trace-ID ratio from `0` to `1`.
- `export_timeout_seconds`, `max_queue_size`, `max_export_batch_size`, and `schedule_delay_millis`: bounded asynchronous export behavior.
- `excluded_paths`: routes such as health probes that should produce no spans.
- `include_tenant_id`: adds the non-secret internal tenant ID when explicitly enabled; it defaults false for privacy.
- `failure_mode`: `open` logs an error and continues without tracing if SDK/exporter initialization fails; `closed` treats tracing as a required policy dependency.

Spans include standardized HTTP and generative-AI operation/provider/model attributes, request ID, status, attempt, and error type. They never include prompts, responses, authorization headers, API keys, request headers, or upstream error bodies. Structured JSON logs automatically add `trace.id` and `span.id` while a span is active.

When `allow_client_bypass` is true, `Cache-Control: no-cache` or `no-store` bypasses both lookup and storage. Disable the feature for sensitive workloads that prohibit response retention. Cached copies omit upstream rate-limit metadata because it becomes stale immediately.

Price fields are metadata expressed in USD per million tokens. Keep prices current when vendors change them.

`accounting.retention_days` controls usage-row retention and must be at least 30 days so tenant rolling spend limits remain enforceable. Cleanup runs during startup.


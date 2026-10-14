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
- `request_deadline_seconds` optionally bounds all attempts against one provider, including establishment of a streaming response. Keep it below the consumer/load-balancer timeout. Streaming requests retry transient failures only before the first response byte; after SSE output begins, the gateway propagates an interruption instead of retrying or failing over and mixing output from multiple attempts.
- Provider `tier` is `free` or `paid`. Free always sorts first.
- Lower model `priority` sorts first within a tier.
- `features.cost_routing` replaces priority-first ordering within each tier with estimated request cost. Free providers still always precede paid providers.
- `unknown_price_policy` places models without pricing last or excludes them. A zero-cost model is known only when it has a `pricing_version`; non-zero legacy prices are treated as known for compatibility.
- `failure_threshold` consecutive errors open that provider's circuit for `cooldown_seconds`.
- `max_concurrency` prevents one upstream account from being overloaded.

## Quotas and models

Each provider may have multiple rolling `limits`. A limit can constrain `requests`, `tokens`, `cost_usd`, or any combination over `window_seconds`. Multiple windows can be combined. Reservations are atomic and persisted in SQLite; dollar limits reserve a conservative worst-case estimate and reconcile against reported input/output usage.

Model aliases decouple clients from vendor model IDs. Capabilities document support for `chat`, `reasoning`, `vision`, `tools`, and `json`. A request with `reasoning_effort` only considers reasoning models.

OpenAI-compatible variants can customize `chat_completions_path`, `api_key_header`, `api_key_prefix`, provider `request_defaults`, model `request_overrides`, and arbitrary headers without adding a new adapter. Client request values take precedence over configured defaults and overrides.

### Least-cost routing

Enable `features.cost_routing` to rank eligible models by the estimated input/output cost of each request after capability, provider override, circuit, and paid-fallback filtering. The sort order is deterministic: tier, known-price status, estimated cost, configured priority, provider name, then upstream model. This preserves strict free-before-paid behavior and stable ties.

```yaml
features:
  cost_routing: true
routing:
  unknown_price_policy: last  # or exclude
providers:
  - name: example
    # ...
    models:
      - name: fast
        upstream_model: provider-model
        input_cost_per_million: 0.40
        output_cost_per_million: 1.60
        pricing_version: "2026-10"
```

Chat token estimates use bounded request parameters and are reconciled with upstream usage for accounting. Embeddings, speech, and image prompts currently rank on estimated input tokens; endpoint-specific unit pricing remains pending. Chat responses include `gateway.routing.strategy`, `estimated_cost_usd`, and `pricing_version`. Pricing is operator-supplied configuration: validate it against provider terms and update the version whenever rates change.

### Latency-aware routing

`features.latency_routing` ranks eligible candidates by a per-provider/model/operation exponentially weighted moving average. It is mutually exclusive with `cost_routing`; capability checks, circuits, provider overrides, paid-fallback policy, and strict free-before-paid tiers still take precedence.

```yaml
features:
  latency_routing: true
routing:
  latency_ewma_alpha: 0.2
  latency_cold_start_ms: 1000
  latency_min_samples: 3
  latency_exploration_interval: 20  # 0 disables exploration
```

Candidates below `latency_min_samples` use the cold-start estimate. Every configured exploration interval, the least-sampled candidate in each tier is tried first; this is counter-based and deterministic rather than random. Only successful upstream responses update EWMA observations. Chat responses expose the selected latency state under `gateway.routing.latency`, and `/admin/providers` exposes content-free observations for troubleshooting.

Observations are in-memory and process-local, reset on restart, and are not coordinated between workers or replicas. This makes the current implementation suitable for a single process and safe experimentation; shared state, percentile-aware ranking, outlier controls, and cross-replica convergence remain pending.

## Responses API

`features.responses_api` exposes `POST /v1/responses`; it is disabled by default in both modes and removed from routing and OpenAPI when disabled. The endpoint translates supported Responses requests into the existing chat pipeline, so authentication, tenant isolation, governance stages, exact caching, idempotency, quotas, free-before-paid routing, retries, and provider failover remain identical.

Supported request behavior includes string or message-array input, instructions, text and image-URL content, function-call history and output, function tools and tool choice, reasoning effort, maximum output tokens, temperature/top-p, parallel tools, and text/JSON/JSON-schema output formats. `responses.max_input_items` and `max_tools` bound adapter work before provider invocation.

The adapter returns response objects with message and function-call output items, usage, incomplete status for token limits, and the gateway's provider metadata. It deliberately rejects streaming, background mode, hosted storage, files, conversations, prompts, and previous-response chaining rather than silently changing their semantics. These limitations remain recorded under `API-RESP-001`; use `/v1/chat/completions` for streaming until Responses SSE translation is implemented and compatibility-tested.

## Embeddings API

`features.embeddings_api` exposes `POST /v1/embeddings`; it is disabled by default in both modes and removed from routing and OpenAPI when disabled. Eligible models must declare `capabilities: [embeddings]`. Each provider may override `embeddings_path`, which defaults to `/embeddings` relative to its OpenAI-compatible `base_url`, and may set embedding-only defaults under `embeddings_request_defaults` without contaminating chat requests.

The endpoint supports non-empty text, text batches, token arrays, `float` or `base64` encoding, optional dimensions, user identifiers, and the diagnostic provider override when globally allowed. `embeddings.max_inputs`, `max_input_characters`, `max_token_ids`, and `max_dimensions` bound work before routing; `allow_token_arrays` can prohibit pre-tokenized input.

```yaml
features:
  embeddings_api: true
providers:
  - name: example
    base_url: https://provider.example/v1
    api_key_env: EXAMPLE_API_KEY
    models:
      - name: embed
        upstream_model: provider-embedding-model
        capabilities: [embeddings]
        input_cost_per_million: 0.10
```

Embedding requests use the standard free-before-paid candidate order, atomic quota reservation, input-price accounting, concurrency limits, retries, failover, circuit breakers, tracing, and provider/model metrics. Input PII and moderation policies apply when enabled. Prompt-injection policy does not apply because embeddings do not execute instructions. Exact response caching and `Idempotency-Key` are not currently supported and idempotency headers are rejected explicitly.

## Moderations API

`features.moderations_api` exposes `POST /v1/moderations`; it is disabled by default in both modes and removed from routing and OpenAPI when disabled. The endpoint authenticates like other `/v1` APIs but runs the configured deterministic local moderation engine without calling an LLM provider, reserving provider quota, or adding upstream latency. Classification is independent of the chat moderation policy: the endpoint always reports findings and never blocks, even when `moderation.action` is `block`.

```yaml
features:
  moderations_api: true
moderations_api:
  max_inputs: 256
  max_input_characters: 1000000
  default_model: gateway-moderation-latest
  accepted_models: [gateway-moderation-latest, omni-moderation-latest]
```

Requests accept one non-empty text string or an array of text strings. Images and other multimodal input types are not yet supported. Unknown fields and `Idempotency-Key` are rejected explicitly. The response follows the OpenAI text-moderation result shape and adds content-free `gateway` metadata for engine version, signal names, and finding count. Category scores are deterministic `0.0`/`1.0` rule-match indicators—not calibrated probabilities. A custom configured signal can set `flagged: true` even when no standard OpenAI category maps to it; inspect `gateway.signals` in that case.

## Audio API

`features.audio_api` exposes `POST /v1/audio/speech`; it is disabled by default in both modes and removed from routing and OpenAPI when disabled. Speech models declare `capabilities: [audio_speech]`. Providers may override `audio_speech_path` and `audio_speech_request_defaults`. The request supports model, input, voice, response format, speed, and optional instructions; configured limits bound input and returned audio bytes.

```yaml
features:
  audio_api: true
audio:
  max_input_characters: 4096
  response_formats: [mp3, opus, aac, flac, wav, pcm]
  max_response_bytes: 25000000
providers:
  - name: speech-provider
    base_url: https://provider.example/v1
    api_key_env: SPEECH_API_KEY
    audio_speech_path: /audio/speech
    models:
      - name: speak
        upstream_model: provider-tts-model
        capabilities: [audio_speech]
```

Speech requests use free-before-paid selection, quotas, concurrency limits, retries, failover, circuits, tracing, and provider metrics. Successful responses contain audio bytes and `X-Gateway-Provider`/`X-Gateway-Upstream-Model` headers. JSON/text success bodies and oversized responses are rejected as malformed. Idempotency is not yet supported. Transcription, translation, streaming audio, input governance, live-provider compatibility, and calibrated character/audio billing remain pending.

## Image generation API

`features.images_api` exposes `POST /v1/images/generations`; it is disabled by default in both modes and removed from routing and OpenAPI when disabled. Image models declare `capabilities: [image_generation]`. Providers may override `image_generations_path` and `image_request_defaults`.

```yaml
features:
  images_api: true
images:
  max_prompt_characters: 32000
  max_images: 4
  max_response_bytes: 50000000
  response_formats: [url, b64_json]
providers:
  - name: image-provider
    base_url: https://provider.example/v1
    api_key_env: IMAGE_API_KEY
    image_generations_path: /images/generations
    models:
      - name: image
        upstream_model: provider-image-model
        capabilities: [image_generation]
```

The endpoint accepts a prompt plus optional model, count, quality, size, style, response format, and user identifier. It validates the returned image count and requires every result to contain the requested URL or base64 field. Oversized and malformed upstream responses trigger provider failover. Requests use normal free-before-paid selection, quotas, concurrency, retries, circuits, tracing, and metrics. Idempotency, prompt governance, image edits/variations, direct binary delivery, provider compatibility CI, and image-specific billing remain pending.

## Model discovery

`features.model_discovery` exposes the administrator-only `POST /admin/model-discovery` endpoint and requires `features.admin_api`. It is disabled by default and performs no background work. Each enabled provider uses `models_path` (default `/models`) when `discovery_enabled` is true.

```yaml
features:
  admin_api: true
  model_discovery: true
model_discovery:
  allow_patterns: ["openai/*", "meta/*"]
  deny_patterns: ["*-preview", "*-deprecated"]
  max_models_per_provider: 10000
  max_concurrency: 8
  approval_mode: report_only
```

The response reports eligible discovered IDs, eligible but unconfigured IDs, and configured upstream IDs absent from the current catalog. Catalog size, identifier length, selected-provider count, and parallel requests are bounded. Errors are isolated per provider and upstream response bodies are never returned. Discovery is deliberately read-only: it does not invent aliases or capabilities, mutate configuration, or activate models. Scheduled discovery, approval persistence, notifications, vendor-specific pagination, and automatic configuration proposals remain pending.

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

## SLA metrics

When `features.metrics` is enabled, `/metrics` exposes Prometheus counters and histograms for gateway HTTP latency, validated provider/model outcomes, every raw upstream attempt, retries, exact-cache outcomes, and provider circuit state. Disabled metrics use a no-op implementation and do not allocate metric state.

`observability.http_latency_buckets_seconds` and `observability.provider_latency_buckets_seconds` define strictly increasing positive histogram buckets. Tune them to the service objectives and expected model latency before production; changing buckets requires restarting the process. Metric labels are deliberately bounded to endpoint, status, provider, public model alias, tier, outcome, cache backend, and retry reason. Tenant identifiers, request IDs, prompts, responses, credentials, and upstream error text are never labels.

The dashboard and alert-rule examples are in `deploy/grafana` and `deploy/prometheus`. Provider results describe fully validated gateway outcomes, while provider requests describe raw HTTP attempts and therefore include same-provider retries.

## PII detection and redaction

`features.pii_detection` inserts a local policy stage before idempotency, caching, quota reservation, and provider routing. It is disabled by default in both modes and its module is not imported when disabled. The built-in engine supports `email`, `phone`, `ipv4`, Luhn-valid `credit_card`, and structurally valid `us_ssn` candidates. `src/llm_gateway/pii.py` defines the `PIIDetector` protocol for additional in-process detectors; third-party plugin loading is intentionally deferred until the extension SDK is versioned.

- `action: redact` replaces findings using `replacement`; `detect` reports metadata without changing content; `block` rejects matching input or suppresses matching output.
- `scan` selects `input`, `output`, or `both`. Input scanning occurs before request fingerprints and cache keys, so persisted data and providers receive the redacted form. Output scanning occurs before response caching.
- `max_scan_characters` bounds work per direction. `failure_mode: closed` rejects content that cannot be fully evaluated; `open` bypasses on scanner errors and records only content-free audit metadata.
- `streaming_output: block` rejects streaming requests when output scanning is required. `bypass` permits the stream and returns `X-PII-Output: BYPASS`; SSE transformation is not claimed because safe cross-chunk matching would require buffering the stream.

Successful non-streaming responses include `gateway.pii` metadata containing only action, count, detector types, and redaction count. Logs and `llm_gateway_pii_events_total` similarly contain no matched values, prompts, tenant identifiers, or request identifiers. Pattern matching can produce false positives and false negatives; tune the enabled detectors and test against representative data. This feature is a data-minimization control, not a compliance certification.

## Moderation policies

`features.moderation` inserts a local moderation stage before PII transformation, idempotency, caching, quota reservation, and provider routing. It is disabled by default in both modes and its module is not imported when disabled. The `ModerationEngine` protocol in `src/llm_gateway/moderation.py` permits provider-neutral in-process engines; versioned third-party loading is deferred to the extension SDK.

The built-in engine is a small deterministic baseline for deployment wiring and organization-specific literal terms. It recognizes conservative phrases in `hate`, `harassment`, `self_harm`, `sexual`, and `violence` categories. Add case-insensitive literal `custom_terms` only when the `custom` category is enabled. It is not a statistical safety model and is not represented as comprehensive moderation.

- `action: monitor` permits content and returns content-free findings metadata; `block` rejects flagged input or suppresses flagged output.
- `scan` chooses `input`, `output`, or `both`; successful non-streaming responses expose only action, flag, count, and category names under `gateway.moderation`.
- `max_scan_characters` bounds evaluation. `failure_mode: closed` rejects unevaluated content; `open` permits it with bypass metadata and an error metric.
- `streaming_output: block` rejects SSE requests when output moderation is required. `bypass` explicitly permits an unmoderated stream and returns `X-Moderation-Output: BYPASS`.

Moderation policy configuration participates in exact-cache invalidation, and output decisions occur before cache writes. Logs and `llm_gateway_moderation_events_total` contain no prompts, responses, matched terms, tenant IDs, or request IDs. Start in monitor mode, evaluate false positives and false negatives against a governed corpus, then enable blocking only with an escalation and appeal process appropriate to the application.

## Prompt-injection risk policy

`features.prompt_injection` enables a local input-only risk stage before moderation, PII transformation, idempotency, caching, quota reservation, and routing. It is disabled by default in both modes and imports no risk engine when disabled. The provider-neutral `PromptInjectionEngine` protocol supports in-process engines; versioned third-party loading is deferred to the extension SDK.

The built-in engine combines bounded heuristic signals for instruction override, secret-exfiltration requests, role override, tool manipulation, and encoding evasion. Repeated matches do not inflate a signal's weight; distinct signal weights combine into a score from zero to one. This score is a configurable operational heuristic, not a probability, classifier guarantee, or prompt-injection security boundary.

- `action: score` records and returns risk metadata without changing behavior.
- `warn` additionally emits a content-free warning log and marks streaming responses with `X-Prompt-Injection-Risk: warning`.
- `block` rejects requests at or above `threshold` before provider or storage work.
- `route` restricts risky requests to models configured with `prompt_injection_safe: true`. Normal requests retain ordinary free-before-paid routing. The marker is an operator assertion about deployment policy, isolation, tool access, or model suitability—not a certification that a model is immune to injection.
- `scan_roles` defaults to untrusted `user` and `tool` messages. `max_scan_characters` bounds work; `failure_mode` controls whether engine failures reject or explicitly bypass policy.

Responses include only action, score, threshold, risky flag, finding count, signal names, and routing requirement under `gateway.prompt_injection`. Metrics and logs never include prompt text, matched phrases, tenant IDs, or request IDs. Policy and model trust settings participate in exact-cache invalidation. Calibrate thresholds against adversarial and ordinary application traffic before blocking or routing production requests.

When `allow_client_bypass` is true, `Cache-Control: no-cache` or `no-store` bypasses both lookup and storage. Disable the feature for sensitive workloads that prohibit response retention. Cached copies omit upstream rate-limit metadata because it becomes stale immediately.

Price fields are metadata expressed in USD per million tokens. Keep prices current when vendors change them.

`accounting.retention_days` controls usage-row retention and must be at least 30 days so tenant rolling spend limits remain enforceable. Cleanup runs during startup.


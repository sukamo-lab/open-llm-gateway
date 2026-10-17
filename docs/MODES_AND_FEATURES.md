# Runtime modes and feature flags

`mode` selects ergonomic defaults; every major subsystem can then be overridden under `features`.

## Single-user mode

```yaml
mode: single
features:
  authentication: false
  admin_api: false
  managed_keys: false
  identity_hierarchy: false
  rbac: false
  oidc: false
  audit_ledger: false
  envelope_encryption: false
  residency_enforcement: false
  tenant_quotas: false
  provider_quotas: false
  usage_accounting: false
  cost_tracking: false
  metrics: false
  circuit_breaker: true
  structured_logging: false
  idempotency: false
  exact_cache: false
  tracing: false
  pii_detection: false
  moderation: false
  prompt_injection: false
  responses_api: false
  embeddings_api: false
  moderations_api: false
  audio_api: false
  images_api: false
  model_discovery: false
  cost_routing: false
  latency_routing: false
  routing_policy: false
  config_reload: false
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
  identity_hierarchy: false
  rbac: false
  oidc: false
  audit_ledger: false
  envelope_encryption: false
  residency_enforcement: false
  tenant_quotas: true
  provider_quotas: true
  usage_accounting: true
  cost_tracking: true
  metrics: true
  circuit_breaker: true
  structured_logging: true
  idempotency: true
  exact_cache: false
  tracing: false
  pii_detection: false
  moderation: false
  prompt_injection: false
  responses_api: false
  embeddings_api: false
  moderations_api: false
  audio_api: false
  images_api: false
  model_discovery: false
  cost_routing: false
  latency_routing: false
  routing_policy: false
  config_reload: false
```

Multi-user defaults enable tenant identities, expiring consumer keys, tenant/provider limits, accounting, costs, metrics, and circuit breaking. PostgreSQL is recommended for multiple replicas.

```powershell
llm-gateway --init --init-mode multi
```

## Feature availability

Disabled features are not merely hidden:

- `admin_api: false` removes every `/admin/*` route.
- `managed_keys: false` avoids identity-store initialization and removes tenant/key routes.
- `identity_hierarchy: false` removes organization/project/principal routes and creates no hierarchy tables unless another identity feature initializes the identity store. It is opt-in in both modes.
- `rbac: false` preserves existing tenant-key behavior. When enabled, unbound and bootstrap keys cannot invoke model APIs; principal role permissions are intersected with the key permission ceiling on authentication.
- `oidc: false` imports no JWT/cryptography package, performs no discovery, and starts no identity-provider network client. When enabled it fails closed and requires RBAC.
- `audit_ledger: false` initializes no audit schema, hashing, export client, or audit endpoints. When enabled, security administration emits content-safe append-only events.
- `envelope_encryption: false` imports no cryptographic implementation and preserves existing persistence behavior. When enabled, persisted cache and idempotency responses use record-bound authenticated envelopes.
- `residency_enforcement: false` preserves ordinary routing-policy selection. When enabled, every covered request is pinned to the configured deny-fallback residency policy and overrides cannot bypass it.
- `metrics: false` removes `/metrics` and uses a no-op metrics object.
- disabling `provider_quotas`, `tenant_quotas`, and `usage_accounting` selects a no-I/O quota implementation.
- `tenant_quotas: false` removes tenant-limit checks from the request path.
- `cost_tracking: false` avoids price calculations and cost persistence.
- `circuit_breaker: false` avoids circuit state bookkeeping.
- `structured_logging: false` removes request correlation and provider log calls from the request path; normal server console logging remains available.
- `idempotency: false` avoids idempotency-store initialization and all key/fingerprint work. When enabled, non-streaming chat requests may use `Idempotency-Key` for tenant-scoped safe replay.
- `exact_cache: false` avoids cache imports, initialization, request hashing, and lookups. The cache is opt-in in both modes because it persists or retains model responses.
- `tracing: false` avoids OpenTelemetry SDK/exporter imports, span creation, context propagation, and background exporter threads. Tracing is opt-in in both modes.
- `pii_detection: false` avoids importing or constructing detectors and performs no prompt traversal. It is opt-in in both modes.
- `moderation: false` avoids importing or constructing policy engines and performs no content traversal. It is opt-in in both modes.
- `prompt_injection: false` avoids importing or constructing risk engines and performs no message traversal. It is opt-in in both modes.
- `responses_api: false` removes `/v1/responses` from routing and OpenAPI. It is opt-in in both modes while compatibility coverage expands.
- `embeddings_api: false` removes `/v1/embeddings` from routing and OpenAPI and initializes no endpoint-specific state.
- `moderations_api: false` removes `/v1/moderations` from routing and OpenAPI and does not construct its classification engine. The endpoint is opt-in in both modes.
- `audio_api: false` removes `/v1/audio/speech` from routing and OpenAPI and adds no audio-specific request processing.
- `images_api: false` removes `/v1/images/generations` from routing and OpenAPI and adds no image-specific request processing.
- `model_discovery: false` removes `/admin/model-discovery` and performs no catalog requests. Discovery also requires `admin_api`.
- `cost_routing: false` retains the original tier/priority ordering and performs no request-cost ranking.
- `latency_routing: false` avoids EWMA observations, selection counters, and latency-based ranking.
- `routing_policy: false` avoids policy matching and preserves the existing candidate order.
- `config_reload: false` removes `/admin/config/reload` and allocates no reload pools or generations.
- `authentication: false` removes consumer authentication checks, but is accepted only on a localhost bind.

Invalid dependency combinations fail configuration validation: managed keys require authentication and the admin API; identity hierarchy requires authentication, the admin API, and managed keys; RBAC requires identity hierarchy; tenant quotas require managed keys; cost tracking requires accounting; configuration reload requires the admin API and cannot be combined with exact caching in its current routing-only form.

## Latency

The gateway creates one shared upstream HTTP connection pool and reuses TLS connections. Single-user minimal mode performs no identity lookup, metrics update, circuit mutation, or quota database transaction. Multi-user authentication uses a bounded TTL cache; PostgreSQL quota reservations remain transactional across replicas. Use `scripts/load_test.py` to measure local overhead and full provider latency independently.

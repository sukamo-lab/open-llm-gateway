# Product roadmap

This roadmap turns the product vision into releasable, independently configurable work. The canonical implementation state is [`FEATURE_STATUS.yaml`](FEATURE_STATUS.yaml); this document explains sequencing and acceptance gates.

## Product principles

1. Preserve a near-zero-complexity single-user path. Enterprise modules default off in single-user mode and must not add database or network I/O when disabled.
2. Keep the OpenAI-compatible request path thin. Expensive policy, semantic-cache, billing, and analytics work must be optional, asynchronous where safe, and benchmarked.
3. Make every externally visible capability configurable. Disabled endpoints are removed from routing and OpenAPI; disabled hot-path modules use no-op implementations.
4. Prefer provider-neutral interfaces and small adapters. Provider-specific behavior belongs in adapters or declarative configuration.
5. Do not mark work complete until code, tests, configuration, operations guidance, security review, and migration/rollback notes exist.

## Definition of done

A feature may move to `implemented` only when all applicable gates pass:

- behavior and failure modes have automated unit tests;
- integration tests cover external boundaries, with deterministic fakes where live credentials are unsuitable;
- its feature flag is tested both enabled and disabled;
- configuration is strictly validated and included in the example configuration;
- API and operator documentation is updated;
- observability exposes useful metrics/logs without secrets or prompt content by default;
- security and tenant-isolation implications are reviewed;
- schema changes have forward and rollback migration instructions;
- hot-path features have latency and resource benchmarks against the disabled baseline;
- `FEATURE_STATUS.yaml` contains evidence and the verification date.

## Delivery plan

### Phase 0 — Engineering foundation (v0.6)

Goal: make subsequent releases safe, measurable, and reproducible.

- `DX-TEST-001`: reliable Windows/Linux CI with unit, integration, lint, type, package, and container smoke jobs.
- `DATA-MIG-001`: versioned SQLite/PostgreSQL migrations and upgrade/rollback commands.
- `OBS-LOG-001`: structured JSON logs with request, tenant, provider, model, attempt, and outcome correlation; prompt bodies off by default.
- `RES-RETRY-001`: bounded exponential retry with jitter and `Retry-After` support within an end-to-end deadline.
- `API-IDEMP-001`: idempotency keys for safe replay and retry behavior.
- `DX-RELEASE-001`: signed/reproducible release artifacts and compatibility policy.

Exit gate: CI is green on Windows and Linux; PostgreSQL integration tests run in CI; upgrades from the previous schema are tested; benchmark baselines are recorded.

### Phase 1 — Immediate enterprise ROI (v0.7)

Goal: reduce spend and make production behavior observable.

- `CACHE-EXACT-001`: tenant-isolated exact cache with canonical request hashing, TTL, size limits, cache-control, and Redis/in-memory backends.
- `OBS-OTEL-001`: OpenTelemetry traces and OTLP export with configurable sampling and content redaction.
- `OBS-SLA-001`: per-provider/model latency histograms, success rates, quota failures, and cache metrics.
- `GOV-PII-001`: pluggable PII detection/redaction with fail-open/fail-closed policy and audit metadata.
- `GOV-MOD-001`: pluggable input/output moderation policies.
- `GOV-INJECT-001`: configurable prompt-injection detection as score, block, warn, or route policy—not an unqualified security guarantee.

Exit gate: disabled overhead is statistically insignificant; exact-cache isolation and redaction have adversarial tests; an example Grafana dashboard and collector configuration are documented.

### Phase 2 — API breadth and intelligent routing (v0.8)

Goal: support modern clients while optimizing selection dynamically.

- `API-RESP-001`, `API-EMBED-001`, `API-MOD-001`: Responses, embeddings, and moderation-compatible endpoints.
- `API-AUDIO-001`, `API-IMAGE-001`: optional audio and image endpoints through capability adapters.
- `DISC-MODEL-001`: scheduled/on-demand model discovery with allowlists, approval mode, and drift reporting.
- `ROUTE-COST-001`: least-cost selection using versioned price metadata.
- `ROUTE-LAT-001`: EWMA and percentile-aware routing with cold-start and exploration controls.
- `ROUTE-POLICY-001`: declarative routing rules for tier, quality, region, residency, provider, and fallback.
- `CFG-RELOAD-001`: validated atomic hot reload with last-known-good rollback.

Exit gate: compatibility tests run against supported OpenAI SDK versions; routing decisions are explainable and reproducible; bad reloads cannot disrupt the active configuration.

### Phase 3 — Enterprise IAM, governance, and audit (v0.9)

Goal: safely delegate operation across organizations.

- `IAM-HIER-001`: organization/project/user/service-account hierarchy.
- `IAM-RBAC-001`: roles, permissions, and scoped keys with deny-by-default authorization.
- `IAM-OIDC-001`: OIDC login and JWT validation; SAML through an identity broker initially.
- `AUDIT-LEDGER-001`: append-only audit events with tamper-evident chaining and external export.
- `SEC-ENC-001`: envelope encryption for sensitive persisted fields with rotation.
- `SEC-CREDROT-001`: overlapping provider credentials and zero-downtime rotation.
- `GOV-RESIDENCY-001`: enforceable region/provider residency attributes and policies.

Exit gate: tenant-boundary and authorization matrices have automated tests; audit export is independently verifiable; threat model and key-rotation runbook are published.

### Phase 4 — FinOps and asynchronous workloads (v1.0)

Goal: provide a commercially operable, scalable gateway.

- `FIN-PLAN-001`: plans, entitlements, pricing versions, credits, and budget alerts.
- `FIN-EXPORT-001`: immutable usage export and reconciliation.
- `FIN-STRIPE-001`: optional Stripe metering/invoicing adapter with idempotent webhook handling.
- `JOB-ASYNC-001`: durable asynchronous jobs, status/cancel endpoints, and signed webhooks.
- `API-BATCH-001`: batch submission built on the job subsystem.
- `RES-DISTCB-001`: distributed circuit and concurrency coordination through Redis/PostgreSQL.
- `CACHE-SEM-001`: opt-in semantic cache with tenant indexes, embedding-version pinning, similarity thresholds, and privacy controls.
- `DEPLOY-HELM-001`: hardened Helm chart, autoscaling, disruption budgets, network policies, and secret-provider examples.
- `OPS-DR-001`: backup, restore, disaster-recovery, and multi-replica upgrade drills.

Exit gate: billing reconciliation and webhook replay tests pass; queue recovery survives worker termination; horizontal load and failover tests meet documented SLOs; restore drills meet stated RPO/RTO.

### Phase 5 — Management experience and ecosystem (v1.x)

- `UI-CONSOLE-001`: optional management console using the same versioned admin API as the CLI.
- `SDK-GEN-001`: generated client SDKs and typed configuration schema.
- `PLUGIN-SDK-001`: versioned provider, policy, storage, cache, and billing extension interfaces.
- `DEPLOY-BIN-001`: signed standalone Windows/Linux binaries after package behavior stabilizes.

## Architectural workstreams

### Request path

Use a compiled request pipeline whose stages are constructed at startup: authenticate, authorize, normalize, guard, cache lookup, reserve quota, select route, invoke with resilience, reconcile usage, cache write, and emit telemetry. Disabled stages are absent rather than branching on every request.

### Extension boundaries

Define protocols for provider adapters, routing strategies, policy engines, cache stores, identity stores, quota/accounting stores, audit sinks, and billing exporters. Keep core request/response models vendor-neutral and version extension interfaces before third-party plugins are supported.

### State and scale

- In-memory/no-op implementations serve minimal single-user mode.
- SQLite supports local and single-replica operation.
- PostgreSQL remains the source of truth for shared durable state.
- Redis is optional for low-latency distributed cache, coordination, and queues; the product must document behavior when Redis is unavailable.

### Performance budgets

Record p50/p95/p99 gateway overhead separately from provider time. Each hot-path phase sets a benchmark budget before implementation. CI should detect material regressions; content scanners and semantic lookup require explicit performance profiles.

## Planning rules

- A target release is intent, not a promise. Dependencies and acceptance gates determine readiness.
- New proposals receive a stable ID in `FEATURE_STATUS.yaml` before implementation.
- Status moves are reviewed like code changes. Never use `implemented` for prototypes, unverified configuration, or documentation-only work.
- Breaking API/configuration changes require deprecation notes and at least one supported migration path.


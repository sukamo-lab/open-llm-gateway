# Changelog

All notable product changes are recorded here. The project follows [Semantic Versioning](https://semver.org/) with the pre-1.0 compatibility rules documented in `docs/COMPATIBILITY.md`.

## [Unreleased]

## [0.10.0] - 2026-10-19

### Added

- First `FIN-PLAN-001` slice: opt-in versioned tenant plans, persistent plan assignment, enforced API entitlements, plan-derived quota ceilings, append-only expiring credit grants, an administrator plan catalog, and usage-backed credit/budget status.
- First `FIN-EXPORT-001` slice: opt-in deterministic completed-usage export with composite cursors, page checksums, bounded reconciliation totals, replay-safe delivery checkpoints, and database-enforced finalized-row immutability.
- First `JOB-ASYNC-001` slice: durable SQLite/PostgreSQL job state, tenant-isolated status/cancellation, leased workers, and a crash-recoverable signed-webhook outbox.
- First `API-BATCH-001` slice: bounded durable non-streaming chat batches with hosted workers, per-item results, status/cancellation, tenant isolation, and explicit sensitive-payload persistence acknowledgement.
- First `RES-DISTCB-001` slice: shared PostgreSQL circuit state and atomic crash-expiring, actively renewed provider concurrency leases.
- First `CACHE-SEM-001` slice: tenant-isolated semantic chat caching with pinned vector versions, request scoping, privacy validation, thresholds, and TTLs.
- First `DEPLOY-HELM-001` slice: hardened Helm chart with migration hooks, autoscaling, disruption budget, network policy, restricted pod security, and external-secret example.
- First `OPS-DR-001` slice: verifiable SQLite backup/manifests, guarded restore, and PostgreSQL/multi-replica disaster-recovery drill guidance.

## [0.9.0] - 2026-10-17

### Added

- Apache-2.0 licensing, maintainer-led governance, controlled contribution guidance, private security reporting, community issue templates, and automated dependency updates.
- Versioned roadmap and canonical feature-state ledger.
- Cross-platform CI and PostgreSQL integration-test definitions.
- Versioned component database migrations with operator commands and rollback guidance.
- Structured JSON logging with request, tenant, and provider correlation.
- Persistent tenant-scoped idempotency for non-streaming chat completions.
- Reproducible, attested, keylessly signed release workflow.
- Tenant-isolated exact response caching with memory, SQLite, PostgreSQL, and Redis backends.
- OpenTelemetry tracing with W3C propagation, OTLP export, and log correlation.
- Provider/model SLA metrics, configurable latency histograms, Prometheus alerts, and a Grafana dashboard.
- Opt-in pluggable PII detection, redaction, blocking, bounded audit metrics, and explicit streaming policy.
- Opt-in provider-neutral moderation policies with local baseline rules, custom terms, and safe audit metrics.
- Opt-in prompt-injection risk scoring, warning, blocking, and operator-designated safe-model routing.
- Feature-flagged Responses API adapter over the existing governed chat execution pipeline.
- Feature-flagged embeddings API with capability routing, validation, accounting, retries, and failover.
- Feature-flagged OpenAI-compatible moderation API using bounded deterministic local text classification.
- Feature-flagged text-to-speech API with bounded input, binary responses, and capability-aware failover.
- Feature-flagged image generation API with bounded prompts, URL/base64 validation, and provider failover.
- Controlled read-only upstream model discovery with bounded catalogs, filters, and drift reports.
- Opt-in deterministic least-cost routing with unknown-price policy and explainable decisions.
- Opt-in latency-aware routing with per-operation EWMA, cold starts, and independently counted deterministic exploration.
- Deadline-aware streaming retries before the first SSE byte, with explicit termination instead of mixed-provider output after a stream starts.
- Opt-in declarative routing policies for tier, provider, region, residency, quality, and explicit fallback behavior.
- Administrator-triggered atomic routing/provider configuration reload with last-known-good rollback.
- Opt-in organization, project, user, and service-account hierarchy with versioned SQLite/PostgreSQL storage.
- Opt-in deny-by-default RBAC with principal-bound, project-scoped API keys and permission ceilings.
- Opt-in fail-closed OIDC discovery/JWT validation with bounded JWKS rotation and brokered SAML guidance.
- Opt-in append-only hash-chained audit events with corruption verification and checkpointed replayable HTTP export.
- Opt-in versioned AES-GCM envelope encryption for persisted cache/idempotency responses with resumable rotation.
- Ordered overlapping provider credential references with safe non-streaming 401/403 fallback during rotation.
- Enforceable named residency routing policy with denied fallback and override-bypass prevention.

### Fixed

- Application helpers can be imported from fresh checkouts and installed packages without requiring a working-directory configuration file.
- Branch CI validates release metadata without misinterpreting the branch name as a release tag.
- PostgreSQL integration coverage follows the authoritative component migration versions.

## [0.5.0] - 2026-10-13

### Added

- OpenAI-compatible chat completions, model listing, and SSE streaming.
- Free-before-paid routing, failover, quotas, circuit breaking, and cost accounting.
- Minimal single-user and managed multi-user profiles.
- Tenant management, revocable consumer keys, SQLite, and PostgreSQL storage.
- Prometheus metrics, diagnostics, Docker, Compose, and operator CLIs.

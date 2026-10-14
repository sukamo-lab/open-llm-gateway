# Changelog

All notable product changes are recorded here. The project follows [Semantic Versioning](https://semver.org/) with the pre-1.0 compatibility rules documented in `docs/COMPATIBILITY.md`.

## [Unreleased]

### Added

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

## [0.5.0] - 2026-10-13

### Added

- OpenAI-compatible chat completions, model listing, and SSE streaming.
- Free-before-paid routing, failover, quotas, circuit breaking, and cost accounting.
- Minimal single-user and managed multi-user profiles.
- Tenant management, revocable consumer keys, SQLite, and PostgreSQL storage.
- Prometheus metrics, diagnostics, Docker, Compose, and operator CLIs.

# Feature state process

`FEATURE_STATUS.yaml` is the canonical snapshot of what the product can do. It is deliberately separate from runtime configuration and must never contain secrets or customer data.

## Status meanings

- `planned`: accepted into the roadmap but not available to users.
- `partial`: some usable behavior exists, but one or more definition-of-done gates are missing. Notes must state the exact limitation.
- `implemented`: available, documented, tested, and verified against the definition of done in `PRODUCT_ROADMAP.md`.
- `deferred`: intentionally removed from the active delivery sequence; notes must explain why.

## Updating state with every feature

The pull request or change set implementing a feature must update the matching ledger entry in the same change:

1. Keep the stable `id`; never recycle an ID for a different capability.
2. Set `status` accurately. Use `partial` until every applicable acceptance gate passes.
3. Add `verified: YYYY-MM-DD` only after verification.
4. Add `evidence` paths for implementation, tests, configuration, and documentation.
5. Record limitations in `notes`, and dependencies in `depends_on`.
6. Update `product_version` and `last_updated` when the product release changes or state moves.
7. Add a release-note entry describing configuration and migration impact.

Suggested entry:

```yaml
- id: CACHE-EXACT-001
  pillar: resilience
  name: Exact tenant-isolated cache
  status: implemented
  verified: 2027-01-15
  evidence:
    - src/llm_gateway/cache.py
    - tests/test_cache.py
    - docs/CONFIGURATION.md
  notes: Redis and bounded in-memory backends; disabled by default.
```

## Review checklist

- The ledger claim matches observable runtime behavior.
- Disabled mode has a test and does not expose endpoints or initialize infrastructure.
- Tenant data, cache keys, metrics, logs, and audit events cannot cross tenant boundaries.
- No evidence path points only to documentation for an `implemented` feature.
- Dependencies marked implemented really are implemented.
- The version and date are updated, and user-facing release notes exist.

## Release snapshot

At each release, tag the repository only after the ledger is reviewed. The tagged `FEATURE_STATUS.yaml` is the immutable historical state for that version. Do not maintain hand-copied historical files; source-control tags and release artifacts preserve them without drift.


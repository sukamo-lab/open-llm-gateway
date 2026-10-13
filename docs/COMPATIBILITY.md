# Compatibility policy

The gateway uses Semantic Versioning for the product package, container tags, configuration contract, and supported public APIs.

## Version guarantees

- Before `1.0`, minor releases may contain necessary breaking changes. Every such change requires changelog notice, migration instructions, and an explicit replacement path. Patch releases remain backward compatible.
- Starting with `1.0`, breaking public API or configuration changes require a major release. Deprecated behavior remains available for at least two minor releases unless retaining it would create a critical security or data-integrity risk.
- Database schemas are forward-migrated by numbered component migrations. Older binaries may run against a newer schema only when the release notes explicitly say so; otherwise restore a pre-upgrade backup as described in `DATABASE_MIGRATIONS.md`.

## Public compatibility surfaces

The following are versioned contracts:

- documented `/v1/*` and `/admin/*` HTTP routes, methods, status codes, and response fields;
- YAML configuration keys and their documented meaning;
- `llm-gateway` and `llm-gateway-admin` command names, exit codes, and JSON output;
- consumer key format and authentication behavior;
- feature IDs in `FEATURE_STATUS.yaml`;
- published Python package names, console entry points, and OCI tag conventions.

Internal Python modules are not a stable application-development API until a plugin SDK explicitly marks interfaces stable.

## OpenAI compatibility

OpenAI-compatible endpoints target the documented request and response shape, but upstream providers may omit or extend fields. The gateway may add a documented `gateway` object and response headers. Provider-specific extensions are pass-through behavior, not portable guarantees. Compatibility tests will pin supported OpenAI SDK versions when broader endpoint coverage is delivered.

## Deprecation process

1. Mark the old behavior deprecated in documentation and the changelog.
2. Emit an operator-visible warning without exposing prompts or credentials.
3. Provide the replacement and migration example.
4. Retain both forms for the stated window.
5. Remove only in the allowed release class and list the removal prominently.

Security fixes may shorten this process. The release notes must explain the risk and required operator action.

## Release artifacts and verification

Official releases originate from an immutable `v<version>` Git tag. Automation verifies that the tag, package metadata, runtime version, feature ledger, and changelog agree. It builds wheel and source archives twice using a pinned direct toolchain and fixed source timestamp, rejecting byte differences.

Published assets include SHA-256 checksums, GitHub artifact attestations, and Sigstore keyless signing bundles. Verify provenance against this repository and confirm checksums before deployment. Release credentials are short-lived GitHub OIDC identities; no long-lived signing key is stored in the repository.

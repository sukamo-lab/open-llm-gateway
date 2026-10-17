# Release runbook

## Prepare

1. Release only from a reviewed commit merged to protected `main`. Require the unit, quality/package, SDK, PostgreSQL, container, dependency-review, and CodeQL checks where applicable.
2. Confirm the repository exposes the Apache-2.0 license, security policy, controlled contribution policy, code of conduct, and support expectations. Keep private vulnerability reporting enabled.
3. Confirm the GitHub `release` environment requires maintainer approval, tag creation is restricted, branch protection prevents direct changes to `main`, and secret scanning/dependency alerts are enabled.
4. Choose the next Semantic Version and update all three version sources:
   - `pyproject.toml` project version;
   - `src/llm_gateway/__init__.py` `__version__`;
   - `docs/FEATURE_STATUS.yaml` `product_version` and `last_updated`.
5. Move relevant `CHANGELOG.md` entries from Unreleased under `## [<version>] - YYYY-MM-DD`.
6. Review feature statuses. A partial capability must remain partial even when included in a release.
7. Run `python scripts/release_check.py metadata`, Ruff, compilation, unit and PostgreSQL tests, package builds, and the installed CLI smoke checks.
8. Build twice with the pinned release toolchain and require byte-for-byte identical artifacts before tagging.

## Publish

Create an annotated, signed Git tag only from the reviewed release commit:

```powershell
git tag -s v0.6.0 -m "LLM Gateway 0.6.0"
git push origin v0.6.0
```

The release workflow refuses a tag that differs from package metadata. It builds twice, compares bytes, creates checksums and provenance, signs packages with GitHub OIDC/Sigstore, uploads workflow artifacts, and creates the GitHub release.

Never tag a feature branch or an unreviewed release-preparation commit. Never move or reuse a published version tag.

Configure a protected GitHub `release` environment with required reviewers. Limit tag creation to release maintainers. The workflow intentionally does not publish to PyPI until a separate trusted-publisher policy is approved.

## Verify

1. Confirm the workflow succeeded and the GitHub release is not a draft.
2. Download assets and validate `SHA256SUMS`.
3. Verify GitHub provenance with `gh attestation verify <artifact> --repo sukamo-lab/open-llm-gateway`.
4. Verify each Sigstore bundle using the identity and issuer documented by the release workflow.
5. Install the wheel in a clean environment and run `llm-gateway --version`, `--check`, and `--doctor` with a disposable configuration.

If any verification fails, do not replace assets under the same tag. Delete the failed release/tag only before users consume it, correct the source, and issue a new version when provenance may already have escaped.

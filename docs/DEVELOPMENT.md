# Development and verification

The supported development baseline is Python 3.11 or newer on Windows and Linux.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

On Linux, activate with `. .venv/bin/activate`. Tests use the repository-local `.test-tmp` directory so they do not depend on operating-system temporary-directory permissions.

## Fast verification

```powershell
python -m pytest -m "not integration" -q
python -m ruff check src tests scripts
python -m compileall -q src scripts tests
python -m build
```

These commands do not require provider credentials and must not call live LLM APIs.

Release metadata is also checked in CI:

```powershell
python scripts/release_check.py metadata
```

The command verifies package/runtime/ledger versions, changelog coverage, and feature evidence paths. Tagged builds additionally require an empty Unreleased section. See [Release runbook](RELEASING.md).

## PostgreSQL integration tests

Start a disposable PostgreSQL instance, set a test-only DSN, and run the integration marker:

```powershell
$env:LLM_GATEWAY_TEST_POSTGRES_DSN = "postgresql://gateway:test-password@localhost:5432/gateway_test"
python -m pytest -m integration -q
```

Never point this variable at a production or shared customer database. Integration tests create schema objects and test records. CI supplies an isolated PostgreSQL service automatically.

## Continuous integration

GitHub Actions runs:

- unit tests on Windows and Linux using the oldest and newest supported Python lines;
- strict marker validation, compilation, and import checks;
- lint and wheel/sdist builds, followed by installed CLI smoke tests;
- PostgreSQL integration tests against a disposable PostgreSQL 17 service;
- a BuildKit container build without pushing an image.

CI jobs receive read-only repository permissions. No provider secrets are needed. Dependency versions are bounded in `pyproject.toml`; release dependency locking and artifact signing are tracked separately by `DX-RELEASE-001`.

## Adding tests

- Unit tests belong in `tests/` and must be deterministic and offline.
- External-service tests belong in `tests/integration/` and use the `integration` marker.
- Live provider tests must be opt-in, must never print credentials or model content, and must not be part of the default CI path.
- Every configurable feature needs tests for both enabled and disabled behavior.

Database changes must be expressed as numbered component migrations in `migrations.py`, include legacy-data preservation tests, and update [Database migrations](DATABASE_MIGRATIONS.md). Never edit an already released migration; add the next version.

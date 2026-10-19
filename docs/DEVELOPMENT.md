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

OpenAI Python SDK compatibility is exercised separately against the latest supported 1.x and 2.x lines:

```powershell
python -m pytest tests/test_openai_sdk.py -q
```

The CI matrix installs `openai>=1.75,<2` and `openai>=2,<3` independently and covers models, chat completions, Responses, embeddings, moderations, speech, and image generation through deterministic in-process provider fakes.

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

An existing PostgreSQL server used by other applications is acceptable only when LLM Gateway receives its own empty database and restricted login. Connect to the server's administrative database as a PostgreSQL administrator:

```powershell
psql -h POSTGRES_HOST -p 5432 -U postgres -d postgres
```

Then run the following in `psql`. `\password` prompts securely instead of storing the new password in shell or SQL history:

```sql
CREATE ROLE llm_gateway_test
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT;
\password llm_gateway_test

CREATE DATABASE llm_gateway_test
  OWNER llm_gateway_test
  ENCODING 'UTF8'
  TEMPLATE template0;

REVOKE ALL ON DATABASE llm_gateway_test FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE llm_gateway_test TO llm_gateway_test;
ALTER DATABASE llm_gateway_test CONNECTION LIMIT 50;
```

Verify the restricted login against the new database:

```powershell
psql -h POSTGRES_HOST -p 5432 -U llm_gateway_test -d llm_gateway_test `
  -c "SELECT current_database(), current_user;"
```

Set the DSN only in the shell running the tests. URL-encode reserved characters in the password, or choose a strong URL-safe generated password:

```powershell
$env:LLM_GATEWAY_TEST_POSTGRES_DSN = "postgresql://llm_gateway_test:URL_ENCODED_PASSWORD@POSTGRES_HOST:5432/llm_gateway_test"
python -m pytest -m integration -q
```

When the test database is no longer needed, disconnect test clients before removing it. These commands permanently delete only the dedicated database and role:

```sql
REVOKE CONNECT ON DATABASE llm_gateway_test FROM llm_gateway_test;
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'llm_gateway_test' AND pid <> pg_backend_pid();
DROP DATABASE llm_gateway_test;
DROP ROLE llm_gateway_test;
```

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

## Phase 3 local overhead baseline

On 2026-10-16, a Windows in-process FastAPI `TestClient` run measured 400 warmed `GET /v1/models` requests. Minimal single-user mode (all Phase 3 features disabled) measured p50 1.107 ms, p95 2.634 ms, and p99 3.376 ms. Managed RBAC with a cached principal-bound key measured p50 1.982 ms, p95 4.416 ms, and p99 4.916 ms: deltas of 0.875 ms, 1.782 ms, and 1.540 ms. These figures include test-client scheduling overhead, are not production capacity claims, and should be repeated on deployment hardware with `scripts/load_test.py`.

On 2026-10-17, the complete local test suite passed with 267 tests passed and 4 environment-dependent tests skipped. Ruff passed, and isolated sdist/wheel builds produced `llm_gateway-0.5.0.tar.gz` and `llm_gateway-0.5.0-py3-none-any.whl`. Hosted CI and live PostgreSQL verification remain separate gates.

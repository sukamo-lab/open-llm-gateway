# LLM Gateway

A self-hosted, OpenAI-compatible control plane for routing LLM traffic across free and paid providers. It runs as a Windows/Linux CLI service or Docker container and keeps applications independent of vendor model names, outages, rate limits, and pricing tiers.

It has two explicit profiles: a minimal single-user mode and a fully managed multi-user mode. Major subsystems are independently feature-flagged and disabled modules are removed from the runtime path.

## Highlights

- OpenAI-compatible chat completions, model listing, and SSE streaming.
- Feature-flagged Responses API compatibility for non-streaming text, vision, reasoning, JSON, and function tools.
- Feature-flagged embeddings API with batching, token-array input, quotas, retries, and free-first failover.
- Feature-flagged OpenAI-compatible text moderation API with bounded batches and no provider call.
- Feature-flagged text-to-speech API with capability routing, quotas, retries, and free-first failover.
- Feature-flagged image generation API with URL/base64 results and capability-aware failover.
- Opt-in read-only upstream model discovery with allow/deny filters and drift reporting.
- Capability-aware routing for chat, reasoning, vision, tools, and JSON workloads.
- Strict free-before-paid ordering with optional prohibition of paid fallback.
- Opt-in least-cost ordering within free/paid tiers using versioned price metadata.
- Opt-in EWMA latency routing with cold-start and deterministic exploration controls.
- Opt-in declarative tier/provider/region/residency/quality routing policies.
- Atomic administrator-triggered routing configuration reload with last-known-good rollback.
- Persistent atomic rolling quotas for requests and tokens.
- Automatic failover plus per-provider circuit breakers and concurrency limits.
- Tenant-scoped persistent idempotency keys for safe non-streaming request replay.
- Opt-in tenant-isolated exact response caching with memory, database, or Redis backends.
- Client and administrator bearer authentication with environment-based secrets.
- Multi-tenant consumer-key issuance, revocation, per-tenant quotas, and spend limits.
- Opt-in organization/project/user/service-account hierarchy for enterprise authorization.
- Provider/model SLA histograms, cache/retry/circuit metrics, and ready-to-import Prometheus alerts and Grafana dashboard.
- Opt-in OpenTelemetry tracing with W3C propagation, OTLP export, and JSON-log correlation.
- Opt-in local PII detection, redaction, or blocking with content-free audit metadata.
- Opt-in input/output moderation with monitor/block policies and custom literal rules.
- Opt-in prompt-injection scoring, warnings, blocking, or restricted-model routing.
- `${ENV}` configuration expansion, model aliases, vendor headers, cost metadata, and safe secret handling.
- Cross-platform Python package, Dockerfile, and Docker Compose deployment.

## Quick start

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
llm-gateway --init
# Edit .env and add GOOGLE_API_KEY (or configure another provider).
llm-gateway --doctor
python -m llm_gateway.cli --config config.yaml
```

### Linux/macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
llm-gateway --init
# Edit .env and add GOOGLE_API_KEY (or configure another provider).
llm-gateway --doctor
llm-gateway --config config.yaml
```

`--init` defaults to the minimal single-user profile. For the managed tenant/key product profile, use `llm-gateway --init --init-mode multi` instead.

Call it with any OpenAI client by using `http://localhost:8000/v1` as the base URL and a gateway client key—not a provider key.

```powershell
$headers = @{ Authorization = "Bearer your-client-key" }
$body = @{
  model = "auto"
  messages = @(@{ role = "user"; content = "Hello" })
} | ConvertTo-Json -Depth 10
Invoke-RestMethod http://localhost:8000/v1/chat/completions `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

To use the Responses compatibility endpoint, enable `features.responses_api` and send `input` instead of `messages`:

```powershell
$body = @{ model = "auto"; input = "Hello" } | ConvertTo-Json
Invoke-RestMethod http://localhost:8000/v1/responses `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

For embeddings, enable `features.embeddings_api`, configure an `embeddings`-capable model, and call:

```powershell
$body = @{ model = "embed"; input = @("first text", "second text") } | ConvertTo-Json
Invoke-RestMethod http://localhost:8000/v1/embeddings `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

For local text classification without an upstream model, enable `features.moderations_api` and call:

```powershell
$body = @{ model = "gateway-moderation-latest"; input = @("Hello", "text to classify") } | ConvertTo-Json
Invoke-RestMethod http://localhost:8000/v1/moderations `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

For text-to-speech, enable `features.audio_api`, configure an `audio_speech`-capable model, and call `POST /v1/audio/speech`.

For image generation, enable `features.images_api`, configure an `image_generation`-capable model, and call `POST /v1/images/generations`.

## Routing behavior

Requests are filtered by public model alias and required capabilities. Eligible free providers are ordered before paid providers; lower model priority wins. The gateway atomically reserves configured quota, tries the best candidate, and fails over on network errors, rate limits, invalid credentials, missing models, server failures, or malformed responses. Repeated failures open a temporary circuit so unhealthy providers stop adding latency.

Use `model: auto` for automatic selection. `reasoning: true` or a non-`none` `reasoning_effort` requires reasoning capability. The optional non-standard `provider` field targets one provider when diagnostic override is enabled.

## Docker

```powershell
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

The Compose volume persists quota history. Put a TLS reverse proxy or cloud load balancer in front of port 8000 for internet deployment.

## Documentation

- [Configuration reference](docs/CONFIGURATION.md)
- [Operations and production checklist](docs/OPERATIONS.md)
- [Windows, Linux, and Docker deployment](docs/DEPLOYMENT.md)
- [Multi-tenancy, key issuance, and horizontal scaling](docs/MULTI_TENANCY.md)
- [Single/multi-user modes and feature flags](docs/MODES_AND_FEATURES.md)
- [Product roadmap and delivery gates](docs/PRODUCT_ROADMAP.md)
- [Canonical feature status](docs/FEATURE_STATUS.yaml)
- [Feature state update process](docs/FEATURE_STATE_PROCESS.md)
- [Development and CI verification](docs/DEVELOPMENT.md)
- [Database migration and rollback operations](docs/DATABASE_MIGRATIONS.md)
- [Compatibility and deprecation policy](docs/COMPATIBILITY.md)
- [Phase 3 threat model](docs/PHASE3_THREAT_MODEL.md)
- [Key-rotation runbook](docs/KEY_ROTATION_RUNBOOK.md)
- [Release preparation and verification](docs/RELEASING.md)
- Interactive API reference: `http://localhost:8000/docs`

## Security

Never expose provider keys to clients. Prefer `api_key_env`, keep `.env` and `config.yaml` out of Git, enable gateway authentication, use a distinct administrator key, disable diagnostic provider override publicly, and terminate TLS before the gateway. Admin endpoints never return provider credentials.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest -m "not integration" -q
python -m ruff check src tests scripts
```

Python 3.11+ is supported. SQLite is appropriate for one gateway instance; use a shared transactional accounting backend before horizontally scaling multiple replicas.

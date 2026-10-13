# Deployment

## Windows

Install the wheel into a dedicated virtual environment and run:

```powershell
$env:LLM_GATEWAY_CONFIG = "C:\ProgramData\llm-gateway\config.yaml"
C:\ProgramData\llm-gateway\.venv\Scripts\llm-gateway.exe --config $env:LLM_GATEWAY_CONFIG
```

For unattended operation, wrap that command with your approved service manager (for example WinSW or NSSM), run it as a restricted service account, grant that account write access only to the data directory, and set provider secrets as service-level environment variables.

## Linux systemd

Example `/etc/systemd/system/llm-gateway.service`:

```ini
[Unit]
Description=LLM Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=llm-gateway
Group=llm-gateway
WorkingDirectory=/opt/llm-gateway
EnvironmentFile=/etc/llm-gateway/gateway.env
ExecStart=/opt/llm-gateway/.venv/bin/llm-gateway --config /etc/llm-gateway/config.yaml
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/llm-gateway

[Install]
WantedBy=multi-user.target
```

Point `database` at `/var/lib/llm-gateway/gateway.db`, then enable with `systemctl enable --now llm-gateway`.

## Docker

The supplied image runs as UID 10001, includes a health check, and writes only to `/app/data`. Compose mounts configuration read-only and persists the database in a named volume. Do not bake `.env` or a secret-bearing `config.yaml` into a published image.

## Reverse proxy

Terminate HTTPS at your ingress and preserve `Authorization`, `X-Request-ID`, and streaming responses. Disable response buffering for `/v1/chat/completions` when using SSE. Apply request body limits appropriate for vision inputs and set an upstream timeout longer than the largest configured provider timeout.

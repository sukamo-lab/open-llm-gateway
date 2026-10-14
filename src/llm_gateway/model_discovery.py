from __future__ import annotations

import asyncio
from fnmatch import fnmatchcase
from typing import Any

import httpx

from .config import GatewayConfig, ModelDiscoveryConfig, ProviderConfig


class ModelDiscoveryError(RuntimeError):
    pass


def parse_model_ids(body: Any, maximum: int) -> list[str]:
    items = body.get("data") if isinstance(body, dict) else body
    if not isinstance(items, list) or len(items) > maximum:
        raise ModelDiscoveryError("upstream returned an invalid or oversized model catalog")
    result: set[str] = set()
    for item in items:
        model_id = item.get("id") if isinstance(item, dict) else item
        if not isinstance(model_id, str) or not model_id or len(model_id) > 256:
            raise ModelDiscoveryError("upstream returned an invalid model identifier")
        result.add(model_id)
    return sorted(result)


def eligible(model_id: str, config: ModelDiscoveryConfig) -> bool:
    return (
        any(fnmatchcase(model_id, pattern) for pattern in config.allow_patterns)
        and not any(fnmatchcase(model_id, pattern) for pattern in config.deny_patterns)
    )


async def discover_provider(
    provider: ProviderConfig,
    config: ModelDiscoveryConfig,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    url = f"{provider.base_url.rstrip('/')}/{provider.models_path.lstrip('/')}"
    credential = provider.api_key() or ""
    header_value = f"{provider.api_key_prefix} {credential}".strip()
    response = await client.get(
        url,
        headers={provider.api_key_header: header_value, **provider.headers},
        timeout=provider.timeout_seconds,
    )
    if response.is_error:
        return {
            "provider": provider.name,
            "status": "error",
            "http_status": response.status_code,
            "error": "upstream catalog request failed",
        }
    try:
        discovered = parse_model_ids(response.json(), config.max_models_per_provider)
    except (ValueError, ModelDiscoveryError) as exc:
        return {"provider": provider.name, "status": "error", "error": str(exc)}
    configured = sorted({model.upstream_model for model in provider.models})
    allowed = [model_id for model_id in discovered if eligible(model_id, config)]
    return {
        "provider": provider.name,
        "status": "ok",
        "approval_mode": config.approval_mode,
        "discovered_count": len(discovered),
        "eligible": allowed,
        "unconfigured": sorted(set(allowed) - set(configured)),
        "configured_missing": sorted(set(configured) - set(discovered)),
    }


async def discover_models(
    gateway: GatewayConfig,
    client: httpx.AsyncClient,
    requested: set[str] | None = None,
) -> list[dict[str, Any]]:
    providers = [
        provider for provider in gateway.providers
        if provider.enabled and provider.discovery_enabled and provider.api_key()
        and (requested is None or provider.name in requested)
    ]
    semaphore = asyncio.Semaphore(gateway.model_discovery.max_concurrency)

    async def bounded(provider: ProviderConfig) -> dict[str, Any]:
        async with semaphore:
            try:
                return await discover_provider(provider, gateway.model_discovery, client)
            except httpx.HTTPError as exc:
                return {
                    "provider": provider.name,
                    "status": "error",
                    "error": type(exc).__name__,
                }

    return list(await asyncio.gather(*(bounded(provider) for provider in providers)))

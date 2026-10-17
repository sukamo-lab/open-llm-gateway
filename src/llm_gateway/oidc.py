from __future__ import annotations

from dataclasses import dataclass
import asyncio
import time
from typing import Any

import httpx


class OIDCValidationError(ValueError):
    pass


@dataclass(frozen=True)
class OIDCIdentity:
    issuer: str
    subject: str
    tenant_id: str
    organization_id: str
    project_id: str | None


class OIDCValidator:
    """Fail-closed OIDC discovery and JWT validation with a bounded JWKS cache."""

    def __init__(self, config, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=config.http_timeout_seconds)
        self._owns_client = client is None
        self._jwks: dict[str, dict[str, Any]] = {}
        self._expires_at = 0.0
        self._jwks_uri: str | None = None
        self._refresh_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _refresh(self) -> None:
        issuer = self.config.issuer.rstrip("/")
        try:
            discovery = await self.client.get(f"{issuer}/.well-known/openid-configuration")
            discovery.raise_for_status()
            metadata = discovery.json()
            if metadata.get("issuer", "").rstrip("/") != issuer:
                raise OIDCValidationError("OIDC discovery issuer mismatch")
            jwks_uri = metadata.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
                if not (self.config.allow_insecure_http and isinstance(jwks_uri, str)):
                    raise OIDCValidationError("OIDC JWKS URI must use HTTPS")
            response = await self.client.get(jwks_uri)
            response.raise_for_status()
            keys = response.json().get("keys", [])
            if not isinstance(keys, list) or len(keys) > self.config.jwks_max_keys:
                raise OIDCValidationError("OIDC JWKS key count is invalid")
            bounded: dict[str, dict[str, Any]] = {}
            for key in keys:
                if not isinstance(key, dict) or not isinstance(key.get("kid"), str):
                    continue
                if key.get("alg") and key["alg"] not in self.config.algorithms:
                    continue
                bounded[key["kid"]] = key
            if not bounded:
                raise OIDCValidationError("OIDC JWKS contains no usable keyed signing keys")
            self._jwks, self._jwks_uri = bounded, jwks_uri
            self._expires_at = time.monotonic() + self.config.jwks_cache_seconds
        except OIDCValidationError:
            raise
        except Exception as exc:
            raise OIDCValidationError("OIDC discovery or JWKS refresh failed") from exc

    async def validate(self, token: str) -> OIDCIdentity:
        if not token or len(token.encode("utf-8")) > self.config.max_token_bytes:
            raise OIDCValidationError("JWT size is invalid")
        try:
            import jwt
        except ImportError as exc:
            raise OIDCValidationError("OIDC support requires the 'oidc' package extra") from exc
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise OIDCValidationError("invalid JWT header") from exc
        algorithm, key_id = header.get("alg"), header.get("kid")
        if algorithm not in self.config.algorithms or not isinstance(key_id, str) or not key_id:
            raise OIDCValidationError("JWT algorithm or key id is not allowed")
        if time.monotonic() >= self._expires_at or key_id not in self._jwks:
            async with self._refresh_lock:
                if time.monotonic() >= self._expires_at or key_id not in self._jwks:
                    await self._refresh()
        key_data = self._jwks.get(key_id)
        if key_data is None:
            raise OIDCValidationError("JWT signing key is unknown")
        try:
            key = jwt.PyJWK.from_dict(key_data, algorithm=algorithm).key
            claims = jwt.decode(
                token, key=key, algorithms=list(self.config.algorithms),
                audience=self.config.audience, issuer=self.config.issuer,
                leeway=self.config.clock_skew_seconds,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except Exception as exc:
            raise OIDCValidationError("JWT signature or claims validation failed") from exc
        values = {
            name: claims.get(getattr(self.config, f"{name}_claim"))
            for name in ("tenant_id", "organization_id", "project_id")
        }
        if not all(isinstance(values[name], str) and values[name] for name in ("tenant_id", "organization_id")):
            raise OIDCValidationError("JWT tenant or organization claim is missing")
        project_id = values["project_id"] if isinstance(values["project_id"], str) and values["project_id"] else None
        return OIDCIdentity(
            issuer=claims["iss"], subject=claims["sub"], tenant_id=values["tenant_id"],
            organization_id=values["organization_id"], project_id=project_id,
        )

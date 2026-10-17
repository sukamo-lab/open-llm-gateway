import sys
from types import SimpleNamespace

import httpx
import pytest

from llm_gateway.config import OIDCConfig
from llm_gateway.oidc import OIDCValidationError, OIDCValidator


class FakePyJWK:
    @staticmethod
    def from_dict(data, algorithm):
        return SimpleNamespace(key=(data["kid"], algorithm))


@pytest.mark.asyncio
async def test_oidc_discovery_validation_and_jwks_rotation(monkeypatch):
    state = {"kid": "first", "refreshes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "issuer": "https://issuer.example", "jwks_uri": "https://issuer.example/jwks",
            })
        state["refreshes"] += 1
        return httpx.Response(200, json={"keys": [{
            "kid": state["kid"], "kty": "RSA", "alg": "RS256", "n": "x", "e": "AQAB",
        }]})

    def decode(token, **kwargs):
        assert kwargs["algorithms"] == ["RS256"]
        assert kwargs["audience"] == "gateway"
        return {
            "iss": "https://issuer.example", "sub": "alice", "aud": "gateway", "exp": 2_000_000_000,
            "tenant_id": "tenant", "organization_id": "org", "project_id": "project",
        }

    fake_jwt = SimpleNamespace(
        get_unverified_header=lambda token: {"alg": "RS256", "kid": token},
        PyJWK=FakePyJWK, decode=decode,
    )
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    validator = OIDCValidator(OIDCConfig(
        issuer="https://issuer.example", audience="gateway", jwks_cache_seconds=300,
    ), client)

    first = await validator.validate("first")
    assert first.subject == "alice" and first.organization_id == "org"
    state["kid"] = "second"
    second = await validator.validate("second")
    assert second.project_id == "project"
    assert state["refreshes"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_oidc_rejects_unsigned_and_unkeyed_tokens(monkeypatch):
    monkeypatch.setitem(sys.modules, "jwt", SimpleNamespace(
        get_unverified_header=lambda token: {"alg": "none"}, PyJWK=FakePyJWK,
    ))
    validator = OIDCValidator(OIDCConfig(issuer="https://issuer.example", audience="gateway"))
    with pytest.raises(OIDCValidationError, match="algorithm or key id"):
        await validator.validate("token")
    await validator.close()


@pytest.mark.asyncio
async def test_oidc_bounds_jwks(monkeypatch):
    monkeypatch.setitem(sys.modules, "jwt", SimpleNamespace(
        get_unverified_header=lambda token: {"alg": "RS256", "kid": "target"}, PyJWK=FakePyJWK,
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "issuer": "https://issuer.example", "jwks_uri": "https://issuer.example/jwks",
            })
        return httpx.Response(200, json={"keys": [
            {"kid": "one", "kty": "RSA"}, {"kid": "two", "kty": "RSA"},
        ]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    validator = OIDCValidator(OIDCConfig(
        issuer="https://issuer.example", audience="gateway", jwks_max_keys=1,
    ), client)
    with pytest.raises(OIDCValidationError, match="key count"):
        await validator.validate("token")
    await client.aclose()

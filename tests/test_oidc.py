from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.api import deps
from app.api.errors import ApiError

ISSUER = "https://idp.test/realms/ovc"
AUDIENCE = "ovc-frontend"


def _keypair(kid: str) -> tuple[rsa.RSAPrivateKey, dict]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid=kid, alg="RS256", use="sig")
    return private, jwk


def _token(private: rsa.RSAPrivateKey, kid: str, **claims: object) -> str:
    now = int(time.time())
    payload = {"sub": "u1", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60}
    payload.update(claims)
    return jwt.encode(payload, private, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake OIDC provider: `keys` is the served JWKS, `fetches` counts downloads."""
    state: dict = {"keys": [], "fetches": 0}

    async def fake_get_jwks(force: bool = False) -> dict:
        state["fetches"] += 1
        return {"keys": list(state["keys"])}

    monkeypatch.setattr(deps, "_get_jwks", fake_get_jwks)
    monkeypatch.setattr(deps._settings, "oidc_jwks_url", "https://idp.test/certs")
    monkeypatch.setattr(deps._settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(deps._settings, "oidc_audience", AUDIENCE)
    return state


async def test_valid_token(provider: dict) -> None:
    private, jwk = _keypair("k1")
    provider["keys"] = [jwk]
    claims = await deps._verify_oidc(_token(private, "k1", email="a@b.c"))
    assert claims["email"] == "a@b.c"


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 10},
        {"aud": "someone-else"},
        {"iss": "https://evil.test"},
    ],
    ids=["expired", "wrong-audience", "wrong-issuer"],
)
async def test_rejected_claims_are_401(provider: dict, claims: dict) -> None:
    private, jwk = _keypair("k1")
    provider["keys"] = [jwk]
    with pytest.raises(ApiError) as exc:
        await deps._verify_oidc(_token(private, "k1", **claims))
    assert exc.value.status_code == 401


async def test_foreign_signature_is_401(provider: dict) -> None:
    _, jwk = _keypair("k1")
    attacker, _ = _keypair("k1")
    provider["keys"] = [jwk]
    with pytest.raises(ApiError) as exc:
        await deps._verify_oidc(_token(attacker, "k1"))
    assert exc.value.status_code == 401


async def test_garbage_token_is_401(provider: dict) -> None:
    with pytest.raises(ApiError) as exc:
        await deps._verify_oidc("not-a-jwt")
    assert exc.value.status_code == 401


async def test_unknown_kid_refetches_jwks(provider: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _, old_jwk = _keypair("old")
    new_private, new_jwk = _keypair("new")
    provider["keys"] = [old_jwk]

    # provider rotated keys after our first fetch
    original = deps._get_jwks

    async def rotating(force: bool = False) -> dict:
        if force:
            provider["keys"] = [new_jwk]
        return await original(force)

    monkeypatch.setattr(deps, "_get_jwks", rotating)
    claims = await deps._verify_oidc(_token(new_private, "new"))
    assert claims["sub"] == "u1"
    assert provider["fetches"] == 2


async def test_unknown_kid_after_refetch_is_401(provider: dict) -> None:
    private, jwk = _keypair("k1")
    provider["keys"] = [jwk]
    with pytest.raises(ApiError) as exc:
        await deps._verify_oidc(_token(private, "nope"))
    assert exc.value.status_code == 401

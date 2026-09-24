from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import User
from ..services.auth import get_or_create_user, resolve_stub_user
from ..services.rbac import VisibleScope, resolve_scope
from .errors import unauthorized

log = logging.getLogger(__name__)
_settings = get_settings()

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


_jwks_cache: dict[str, object] = {"at": 0.0, "doc": None}
# floor between forced refreshes, so tokens with bogus `kid`s can't hammer the provider
_JWKS_MIN_REFRESH_SECONDS = 60


async def _get_jwks(force: bool = False) -> dict:
    """Fetch the OIDC provider's JWKS, cached for oidc_jwks_ttl_seconds.

    `force` re-downloads before the TTL expires, at most once per
    _JWKS_MIN_REFRESH_SECONDS.
    """
    now = time.monotonic()
    age = now - float(_jwks_cache["at"])
    ttl = _JWKS_MIN_REFRESH_SECONDS if force else _settings.oidc_jwks_ttl_seconds
    if _jwks_cache["doc"] is not None and age < ttl:
        return _jwks_cache["doc"]  # type: ignore[return-value]

    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        doc = (await client.get(_settings.oidc_jwks_url)).json()
    _jwks_cache["doc"] = doc
    _jwks_cache["at"] = now
    return doc


def _find_key(jwks: dict, kid: str | None):
    """Signing key from the JWKS matching the token's `kid` (or the only key)."""
    from jwt import PyJWKSet

    keys = PyJWKSet.from_dict(jwks).keys
    if kid is None:
        return keys[0] if len(keys) == 1 else None
    return next((k for k in keys if k.key_id == kid), None)


async def _verify_oidc(token: str) -> dict:
    import jwt

    if not _settings.oidc_jwks_url:
        raise unauthorized("OIDC JWKS URL not configured")

    try:
        kid = jwt.get_unverified_header(token).get("kid")
        key = _find_key(await _get_jwks(), kid)
        if key is None:
            # unknown kid - the provider may have rotated keys since the last fetch
            key = _find_key(await _get_jwks(force=True), kid)
        if key is None:
            raise unauthorized("Token signing key not found")
        return jwt.decode(
            token,
            key,
            algorithms=[key.algorithm_name],
            audience=_settings.oidc_audience or None,
            issuer=_settings.oidc_issuer or None,
            options={"verify_aud": bool(_settings.oidc_audience)},
        )
    except jwt.PyJWTError as exc:
        raise unauthorized(f"Invalid token: {exc}") from exc


def _claim_by_path(claims: dict, path: str) -> object:
    """Walk a dot-path (`a.b.c`) into the decoded token."""
    node: object = claims
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _token_roles(claims: dict) -> list[str]:
    """Roles array at oidc_roles_claim (array, or space-delimited string)."""
    path = _settings.oidc_roles_claim.replace("${client_id}", _settings.oidc_client_id)
    value = _claim_by_path(claims, path)
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return value.split()
    return []


async def get_current_user(request: Request, db: DbSession) -> User:
    if _settings.auth_mode == "stub":
        return await resolve_stub_user(db)

    token = _bearer_token(request)
    if not token:
        raise unauthorized()
    claims = await _verify_oidc(token)
    return await get_or_create_user(
        db,
        email=claims.get("email") or claims["sub"],
        display_name=claims.get("name"),
        roles=_token_roles(claims),
        grant_global=False,
    )


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_scope(user: CurrentUser, db: DbSession) -> VisibleScope:
    return await resolve_scope(db, user)


Scope = Annotated[VisibleScope, Depends(get_scope)]

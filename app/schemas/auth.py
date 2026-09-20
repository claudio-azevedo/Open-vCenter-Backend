from __future__ import annotations

from .common import CamelModel


class ScopeOut(CamelModel):
    level: str
    scope_id: str | None = None


class MeOut(CamelModel):
    id: str
    email: str
    display_name: str | None = None
    roles: list[str] = []
    scopes: list[ScopeOut] = []


class HealthOut(CamelModel):
    ok: bool
    db: bool
    cache: bool
    rabbit: bool

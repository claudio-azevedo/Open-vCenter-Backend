from __future__ import annotations

from fastapi import APIRouter

from ...schemas import MeOut, ScopeOut
from ..deps import CurrentUser, Scope

router = APIRouter(tags=["auth"])


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser, scope: Scope) -> MeOut:
    roles = user.roles.split(",") if user.roles else []
    if scope.all:
        scopes = [ScopeOut(level="global", scope_id=None)]
    else:
        scopes = (
            [ScopeOut(level="cluster", scope_id=c) for c in sorted(scope.cluster_ids)]
            + [ScopeOut(level="host", scope_id=h) for h in sorted(scope.host_ids)]
            + [ScopeOut(level="folder", scope_id=f) for f in sorted(scope.folder_ids)]
        )
    return MeOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=roles,
        scopes=scopes,
    )

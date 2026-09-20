from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from ...models import Vlan
from ...schemas import VlanCreate, VlanOut, VlanUpdate
from ...services.networking import (
    create_vlan,
    delete_vlan,
    list_vlans,
    update_vlan,
)
from ...services.serializers import vlan_out
from ..deps import DbSession, Scope
from ..errors import forbidden, not_found

router = APIRouter(tags=["vlans"])


def _visible(scope: Scope, vlan: Vlan) -> bool:
    return (
        scope.all
        or (vlan.cluster_id is not None and scope.sees_cluster(vlan.cluster_id))
        or (vlan.host_id is not None and scope.sees_host(vlan.host_id))
    )


@router.get("/vlans", response_model=list[VlanOut])
async def get_vlans(
    db: DbSession,
    scope: Scope,
    host_id: str | None = Query(default=None, alias="hostId"),
    cluster_id: str | None = Query(default=None, alias="clusterId"),
) -> list[VlanOut]:
    vlans = await list_vlans(db, host_id=host_id, cluster_id=cluster_id)
    return [vlan_out(v) for v in vlans if _visible(scope, v)]


@router.post("/vlans", response_model=VlanOut, status_code=status.HTTP_201_CREATED)
async def add_vlan(body: VlanCreate, db: DbSession, scope: Scope) -> VlanOut:
    allowed = scope.all
    if body.cluster_id:
        allowed = allowed or scope.sees_cluster(body.cluster_id)
    if body.host_id:
        allowed = allowed or scope.sees_host(body.host_id)
    if not allowed:
        raise forbidden("You cannot create a VLAN here")
    vlan = await create_vlan(
        db,
        name=body.name,
        vlan_id=body.vlan_id,
        description=body.description,
        is_default=body.is_default,
        cluster_id=body.cluster_id,
        host_id=body.host_id,
    )
    return vlan_out(vlan)


async def _get_visible_vlan(db: DbSession, scope: Scope, vlan_id: str) -> Vlan:
    vlan = await db.get(Vlan, vlan_id)
    if vlan is None:
        raise not_found("VLAN")
    if not _visible(scope, vlan):
        raise forbidden()
    return vlan


@router.patch("/vlans/{vlan_id}", response_model=VlanOut)
async def patch_vlan(
    vlan_id: str, body: VlanUpdate, db: DbSession, scope: Scope
) -> VlanOut:
    vlan = await _get_visible_vlan(db, scope, vlan_id)
    vlan = await update_vlan(
        db,
        vlan,
        name=body.name,
        description=body.description,
        is_default=body.is_default,
    )
    return vlan_out(vlan)


@router.delete("/vlans/{vlan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_vlan(vlan_id: str, db: DbSession, scope: Scope) -> Response:
    vlan = await _get_visible_vlan(db, scope, vlan_id)
    await delete_vlan(db, vlan)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

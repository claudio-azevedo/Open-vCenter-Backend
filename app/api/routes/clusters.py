from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import func, select

from ...models import Cluster, Host, Vm
from ...schemas import ClusterCreate, ClusterOut, ClusterUpdate
from ...services.organization import create_cluster, delete_cluster, rename_cluster
from ..deps import DbSession, Scope
from ..errors import forbidden, not_found

router = APIRouter(tags=["clusters"])


async def _counts(db: DbSession) -> dict[str, tuple[int, int]]:
    rows = (
        await db.execute(
            select(
                Host.cluster_id,
                func.count(func.distinct(Host.id)),
                func.count(func.distinct(Vm.id)),
            )
            .select_from(Host)
            .outerjoin(Vm, Vm.host_id == Host.id)
            .group_by(Host.cluster_id)
        )
    ).all()
    return {cid: (hc, vc) for cid, hc, vc in rows if cid is not None}


def _cluster_out(cluster: Cluster, counts: dict[str, tuple[int, int]]) -> ClusterOut:
    hc, vc = counts.get(cluster.id, (0, 0))
    return ClusterOut(
        id=cluster.id,
        name=cluster.name,
        hypervisor=cluster.hypervisor,
        host_count=hc,
        vm_count=vc,
    )


@router.get("/clusters", response_model=list[ClusterOut])
async def list_clusters(db: DbSession, scope: Scope) -> list[ClusterOut]:
    clusters = (await db.execute(select(Cluster).order_by(Cluster.name))).scalars().all()
    counts = await _counts(db)
    return [_cluster_out(c, counts) for c in clusters if scope.sees_cluster(c.id)]


@router.post("/clusters", response_model=ClusterOut, status_code=status.HTTP_201_CREATED)
async def add_cluster(body: ClusterCreate, db: DbSession, scope: Scope) -> ClusterOut:
    if not scope.all:
        raise forbidden("Only an administrator can create clusters")
    cluster = await create_cluster(db, name=body.name, hypervisor=body.hypervisor)
    return _cluster_out(cluster, {})


@router.get("/clusters/{cluster_id}", response_model=ClusterOut)
async def get_cluster(cluster_id: str, db: DbSession, scope: Scope) -> ClusterOut:
    cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        raise not_found("Cluster")
    if not scope.sees_cluster(cluster.id):
        raise forbidden()
    return _cluster_out(cluster, await _counts(db))


async def _admin_cluster(db: DbSession, scope: Scope, cluster_id: str) -> Cluster:
    if not scope.all:
        raise forbidden("Only an administrator can manage clusters")
    cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        raise not_found("Cluster")
    return cluster


@router.patch("/clusters/{cluster_id}", response_model=ClusterOut)
async def edit_cluster(
    cluster_id: str, body: ClusterUpdate, db: DbSession, scope: Scope
) -> ClusterOut:
    cluster = await _admin_cluster(db, scope, cluster_id)
    cluster = await rename_cluster(db, cluster, body.name)
    return _cluster_out(cluster, await _counts(db))


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_cluster(cluster_id: str, db: DbSession, scope: Scope) -> None:
    cluster = await _admin_cluster(db, scope, cluster_id)
    await delete_cluster(db, cluster)

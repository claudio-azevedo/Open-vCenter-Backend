from __future__ import annotations

import logging

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.errors import ApiError, not_found
from ..ids import new_id
from ..models import DEFAULT_HYPERVISOR, Cluster, Folder, Host, Vm
from .hosts import deprovision_host_messaging

log = logging.getLogger(__name__)


# ---- clusters ---------------------------------------------------------------

async def create_cluster(
    db: AsyncSession, *, name: str, hypervisor: str = DEFAULT_HYPERVISOR
) -> Cluster:
    cluster = Cluster(
        id=new_id(), name=name.strip(), hypervisor=str(hypervisor)
    )
    db.add(cluster)
    await db.flush()
    return cluster


async def rename_cluster(db: AsyncSession, cluster: Cluster, name: str) -> Cluster:
    cluster.name = name.strip()
    await db.flush()
    return cluster


async def delete_cluster(db: AsyncSession, cluster: Cluster) -> None:
    """Remove an empty cluster. Any cluster-scoped folders go with it (FK cascade);
    it must have no member hosts - move or remove those first."""
    host_count = await db.scalar(
        select(func.count()).select_from(Host).where(Host.cluster_id == cluster.id)
    )
    if host_count:
        raise ApiError(
            "INVALID",
            "This cluster still has hosts - move or remove them first",
        )
    await db.delete(cluster)
    await db.flush()
    log.info("deleted cluster=%s", cluster.id)


# ---- hosts: cluster membership -------------------------------------------

async def move_host(db: AsyncSession, host: Host, cluster_id: str | None) -> Host:
    """Attach a host to a cluster or make it standalone. Folder assignments don't
    survive the move: folders belong to the old cluster/host, so every VM on this
    host is pulled out of its folder, and any host-scoped folders are dropped."""
    target = await db.get(Cluster, cluster_id) if cluster_id is not None else None
    if cluster_id is not None and target is None:
        raise not_found("Cluster")
    if target is not None and target.hypervisor != host.hypervisor:
        raise ApiError(
            "INVALID",
            f"This host runs {host.hypervisor}; the cluster runs {target.hypervisor}",
        )
    if host.cluster_id == cluster_id:
        return host

    host.cluster_id = cluster_id
    # keep the denormalised Vm.cluster_id in step; folders don't survive the move
    await db.execute(
        update(Vm)
        .where(Vm.host_id == host.id)
        .values(cluster_id=cluster_id, folder_id=None)
    )
    await db.execute(delete(Folder).where(Folder.host_id == host.id))
    await db.flush()
    log.info("moved host=%s -> cluster=%s", host.id, cluster_id or "(standalone)")
    return host


async def delete_host(db: AsyncSession, host: Host) -> None:
    """Remove a host and everything under it - its VMs, host-scoped folders,
    templates and ISOs go with it via FK cascade. The host's RabbitMQ queues and
    agent user are torn down too (best-effort)."""
    host_id, host_ref, agent_rmq_user = host.id, host.short_id, host.agent_rmq_user
    await db.delete(host)
    await db.flush()
    await deprovision_host_messaging(host_ref, agent_rmq_user)
    log.info("deleted host=%s", host_id)


# ---- folders ----------------------------------------------------------

async def create_folder(
    db: AsyncSession,
    *,
    name: str,
    cluster_id: str | None,
    host_id: str | None,
) -> Folder:
    if bool(cluster_id) == bool(host_id):
        raise ApiError("INVALID", "Provide exactly one of clusterId or hostId")

    if cluster_id is not None:
        if await db.get(Cluster, cluster_id) is None:
            raise not_found("Cluster")
    else:
        host = await db.get(Host, host_id)
        if host is None:
            raise not_found("Host")
        if host.cluster_id is not None:
            raise ApiError(
                "INVALID",
                "This host is in a cluster - create the folder on the cluster instead",
            )

    folder = Folder(
        id=new_id(),
        name=name.strip(),
        cluster_id=cluster_id,
        host_id=host_id,
    )
    db.add(folder)
    await db.flush()
    return folder


async def rename_folder(db: AsyncSession, folder: Folder, name: str) -> Folder:
    folder.name = name.strip()
    await db.flush()
    return folder


async def delete_folder(db: AsyncSession, folder: Folder) -> None:
    await db.execute(
        update(Vm).where(Vm.folder_id == folder.id).values(folder_id=None)
    )
    await db.delete(folder)
    await db.flush()


# ---- move a VM into / out of a folder ----------------------------------

async def move_vm_to_folder(
    db: AsyncSession, vm: Vm, folder_id: str | None
) -> Vm:
    if folder_id is None:
        vm.folder_id = None
        await db.flush()
        return vm

    folder = await db.get(Folder, folder_id)
    if folder is None:
        raise not_found("Folder")

    host = await db.get(Host, vm.host_id)
    assert host is not None

    if folder.cluster_id is not None:
        if host.cluster_id != folder.cluster_id:
            raise ApiError(
                "INVALID",
                "That folder belongs to a different cluster than this VM's host",
            )
    else:  # host-scoped folder
        if host.cluster_id is not None or folder.host_id != host.id:
            raise ApiError(
                "INVALID", "That folder does not belong to this VM's host"
            )

    vm.folder_id = folder.id
    await db.flush()
    return vm


# ---- helpers for RBAC / tree building -------------------------------

async def cluster_host_ids(db: AsyncSession, cluster_id: str) -> list[str]:
    return list(
        (
            await db.execute(select(Host.id).where(Host.cluster_id == cluster_id))
        ).scalars().all()
    )

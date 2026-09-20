from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...cache import (
    get_vm_lock,
    get_vm_locks,
    get_vm_state,
    get_vm_states,
    list_vm_locks,
    release_all_vm_locks,
    release_vm_lock,
)
from ...config import get_settings
from ...models import Host, Task, Vm, VmMetric
from ...schemas import (
    TaskEnvelope,
    VmActionRequest,
    VmClone,
    VmCreate,
    VmCreateEnvelope,
    VmLockEntry,
    VmMetricSample,
    VmMove,
    VmOut,
)
from ...services.organization import move_vm_to_folder
from ...services.rbac import vm_visible
from ...services.serializers import agent_connected, task_out, vm_metric_out, vm_out
from ...services.vms import (
    ACTION_MAP,
    remove_vm_from_inventory,
    request_vm_action,
    request_vm_clone,
    request_vm_create,
)
from ..deps import CurrentUser, DbSession, Scope
from ..errors import ApiError, forbidden, not_found

router = APIRouter(tags=["vms"])

_ALLOWED_ACTIONS = tuple(a for a in ACTION_MAP if a != "delete")


async def _offline_host_ids(db: DbSession, host_ids: set[str]) -> set[str]:
    """Of the given host ids, the ones whose agent has gone offline - their VMs
    read as ``Unknown``."""
    if not host_ids:
        return set()
    hosts = (
        await db.execute(select(Host).where(Host.id.in_(host_ids)))
    ).scalars().all()
    return {h.id for h in hosts if not agent_connected(h)}


async def _host_offline(db: DbSession, host_id: str) -> bool:
    host = await db.get(Host, host_id)
    return host is not None and not agent_connected(host)


@router.get("/vms", response_model=list[VmOut])
async def list_vms(
    db: DbSession,
    scope: Scope,
    host_id: str | None = Query(default=None, alias="hostId"),
    folder_id: str | None = Query(default=None, alias="folderId"),
    state: str | None = Query(default=None),
) -> list[VmOut]:
    stmt = (
        select(Vm)
        .options(selectinload(Vm.disks), selectinload(Vm.nics))
        .order_by(Vm.name)
    )
    if host_id is not None:
        stmt = stmt.where(Vm.host_id == host_id)
    if folder_id is not None:
        stmt = stmt.where(Vm.folder_id == folder_id)
    if state is not None:
        stmt = stmt.where(Vm.state == state)

    vms = [v for v in (await db.execute(stmt)).scalars().all() if vm_visible(scope, v)]
    ids = [v.id for v in vms]
    states = await get_vm_states(ids)
    locks = await get_vm_locks(ids)
    offline = await _offline_host_ids(db, {v.host_id for v in vms})
    result = [
        vm_out(
            v,
            states.get(v.id),
            locks.get(v.id),
            host_offline=v.host_id in offline,
        )
        for v in vms
    ]
    if state is not None:
        # re-filter against the cache overlay
        result = [v for v in result if v.state == state]
    return result


@router.post(
    "/vms",
    response_model=VmCreateEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def create_vm(
    body: VmCreate, db: DbSession, scope: Scope, user: CurrentUser
) -> VmCreateEnvelope:
    host = await db.get(Host, body.host_id)
    if host is None:
        raise not_found("Host")
    if not scope.sees_host(host.id):
        raise forbidden("You cannot create a VM on this host")
    vm, task = await request_vm_create(db, host, body, requested_by=user.email)
    return VmCreateEnvelope(
        vm=vm_out(vm, lock=await get_vm_lock(vm.id)), task=task_out(task)
    )


async def _get_visible_vm(db: DbSession, scope: Scope, vm_id: str) -> Vm:
    vm = (
        await db.execute(
            select(Vm)
            .where(Vm.id == vm_id)
            .options(selectinload(Vm.disks), selectinload(Vm.nics))
        )
    ).scalar_one_or_none()
    if vm is None:
        raise not_found("VM")
    if not vm_visible(scope, vm):
        raise forbidden()
    return vm


@router.get("/vm-locks", response_model=list[VmLockEntry])
async def list_locks(db: DbSession, scope: Scope) -> list[VmLockEntry]:
    """Every VM currently locked by a running operation. Admin only - this is a
    cross-scope view for unsticking stale locks."""
    if not scope.all:
        raise forbidden("Only administrators can view VM locks")
    locks = await list_vm_locks()
    if not locks:
        return []

    vm_ids = [lk["vm_id"] for lk in locks]
    names = {
        r.id: r.name
        for r in (
            await db.execute(select(Vm.id, Vm.name).where(Vm.id.in_(vm_ids)))
        ).all()
    }
    task_ids = [lk["task_id"] for lk in locks]
    statuses = {
        r.id: r.status
        for r in (
            await db.execute(
                select(Task.id, Task.status).where(Task.id.in_(task_ids))
            )
        ).all()
    }
    return [
        VmLockEntry(
            vm_id=lk["vm_id"],
            vm_name=names.get(lk["vm_id"]),
            task_id=lk["task_id"],
            kind=lk["kind"],
            requested_by=lk["requested_by"],
            acquired_at=lk["acquired_at"],
            ttl=lk["ttl"],
            task_status=statuses.get(lk["task_id"]),
        )
        for lk in locks
    ]


@router.delete("/vm-locks", status_code=status.HTTP_200_OK)
async def release_locks(scope: Scope) -> dict:
    """Force-release every VM lock. Use only when locks are known to be stale."""
    if not scope.all:
        raise forbidden("Only administrators can release VM locks")
    released = await release_all_vm_locks()
    return {"released": released}


@router.delete("/vm-locks/{vm_id}", status_code=status.HTTP_200_OK)
async def release_lock(vm_id: str, scope: Scope) -> dict:
    """Force-release one VM's lock (stale-lock recovery). Works even if the VM
    row is already gone."""
    if not scope.all:
        raise forbidden("Only administrators can release VM locks")
    released = await release_vm_lock(vm_id)
    return {"released": bool(released)}


@router.get("/vms/{vm_id}", response_model=VmOut)
async def get_vm(vm_id: str, db: DbSession, scope: Scope) -> VmOut:
    vm = await _get_visible_vm(db, scope, vm_id)
    return vm_out(
        vm,
        await get_vm_state(vm.id),
        await get_vm_lock(vm.id),
        host_offline=await _host_offline(db, vm.host_id),
    )


@router.get("/vms/{vm_id}/metrics", response_model=list[VmMetricSample])
async def list_vm_metrics(
    vm_id: str, db: DbSession, scope: Scope
) -> list[VmMetricSample]:
    """Quick VM metrics for the last hour, oldest first. Empty until Hyper-V
    resource metering is enabled on the VM (see the enable_metrics action)."""
    await _get_visible_vm(db, scope, vm_id)
    since = datetime.now(UTC) - timedelta(
        seconds=get_settings().metrics_retention_seconds
    )
    rows = (
        await db.execute(
            select(VmMetric)
            .where(VmMetric.vm_id == vm_id)
            .where(VmMetric.ts >= since)
            .order_by(VmMetric.ts)
        )
    ).scalars().all()
    return [vm_metric_out(r) for r in rows]


@router.patch("/vms/{vm_id}", response_model=VmOut)
async def move_vm(
    vm_id: str, body: VmMove, db: DbSession, scope: Scope
) -> VmOut:
    """Move the VM into a folder (folderId) or out of any folder (folderId=null).
    This is a purely logical operation - no agent request."""
    vm = await _get_visible_vm(db, scope, vm_id)
    vm = await move_vm_to_folder(db, vm, body.folder_id)
    return vm_out(
        vm,
        await get_vm_state(vm.id),
        await get_vm_lock(vm.id),
        host_offline=await _host_offline(db, vm.host_id),
    )


@router.post(
    "/vms/{vm_id}/actions/{action}",
    response_model=TaskEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def vm_action(
    vm_id: str,
    action: str,
    db: DbSession,
    scope: Scope,
    user: CurrentUser,
    body: VmActionRequest | None = None,
) -> TaskEnvelope:
    if action not in _ALLOWED_ACTIONS:
        raise ApiError(
            "UNKNOWN_ACTION",
            f"Unknown VM action '{action}'",
            status.HTTP_404_NOT_FOUND,
        )
    vm = await _get_visible_vm(db, scope, vm_id)
    task = await request_vm_action(
        db,
        vm,
        action,
        requested_by=user.email,
        params=body.params if body else None,
    )
    return TaskEnvelope(task=task_out(task))


@router.post(
    "/vms/clone",
    response_model=VmCreateEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def clone_vm(
    body: VmClone, db: DbSession, scope: Scope, user: CurrentUser
) -> VmCreateEnvelope:
    """Provision a new VM by cloning an off VM (`source="vm"`) or deploying an
    exported template (`source="template"`). Inserts a placeholder VM row, queues
    a `vm_clone` task and publishes it to the target host's agent."""
    host = await db.get(Host, body.host_id)
    if host is None:
        raise not_found("Host")
    if not scope.sees_host(host.id):
        raise forbidden("You cannot create a VM on this host")
    vm, task = await request_vm_clone(db, body, host, requested_by=user.email)
    return VmCreateEnvelope(
        vm=vm_out(vm, lock=await get_vm_lock(vm.id)), task=task_out(task)
    )


@router.delete(
    "/vms/{vm_id}", response_model=TaskEnvelope, status_code=status.HTTP_202_ACCEPTED
)
async def delete_vm(
    vm_id: str,
    db: DbSession,
    scope: Scope,
    user: CurrentUser,
    remove_files: bool = Query(
        default=False,
        description="Also delete the VM's folder and files from the host's disk "
        "(irreversible). Default: only remove the VM from Hyper-V.",
    ),
) -> TaskEnvelope:
    vm = await _get_visible_vm(db, scope, vm_id)
    task = await request_vm_action(
        db,
        vm,
        "delete",
        requested_by=user.email,
        params={"remove_files": remove_files},
    )
    return TaskEnvelope(task=task_out(task))


@router.delete("/vms/{vm_id}/from-inventory", status_code=status.HTTP_200_OK)
async def forget_vm(vm_id: str, db: DbSession, scope: Scope) -> dict:
    """Remove the VM record from the database only - no agent request. For a VM
    left ``Unknown`` on a host that is gone for good. Refused (409 `HOST_ONLINE`)
    while the host agent is still reporting the VM."""
    vm = await _get_visible_vm(db, scope, vm_id)
    await remove_vm_from_inventory(db, vm)
    return {"removed": True}

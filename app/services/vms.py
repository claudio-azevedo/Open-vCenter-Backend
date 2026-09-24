from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..api.errors import ApiError, vm_locked
from ..cache import (
    acquire_vm_lock,
    clear_vm_state,
    get_vm_lock,
    release_vm_lock,
    set_vm_state,
)
from ..ids import new_id
from ..messaging import AgentRequest, publish_request
from ..models import Host, Task, Template, Vm, VmNic
from ..schemas import VmClone, VmCreate
from .host_actions import host_is_clustered
from .serializers import agent_connected

log = logging.getLogger(__name__)


def _require_host_online(host: Host) -> None:
    """VM operations need the host agent reachable. A host that stopped checking
    in reports all its VMs as ``Unknown`` - reject the action with a clear 409
    rather than queueing a task that can only time out."""
    if not agent_connected(host):
        raise ApiError(
            "HOST_OFFLINE",
            f"Host '{host.name}' agent is offline - VM actions are unavailable "
            "until it reconnects",
            status_code=409,
        )


_CSV_RE = re.compile(r"^[a-z]:\\clusterstorage\\[^\\]+", re.IGNORECASE)


def _norm_path(path: str | None) -> str:
    return (path or "").strip().replace("/", "\\").rstrip("\\")


def _is_csv_path(path: str) -> bool:
    return bool(_CSV_RE.match(path))


def _is_system_drive(path: str) -> bool:
    """A local path on C: - the host's system volume (a CSV under
    C:\\ClusterStorage is not the system volume)."""
    return path[:2].lower() == "c:" and not _is_csv_path(path)


def _require_vm_storage_allowed(host: Host, destination: str | None) -> None:
    """Reject a VM placement the platform does not allow:

    - clustered host: only Cluster Shared Volumes;
    - standalone host: never the system drive (C:), unless the Hyper-V default
      VM path is on it - the agent then places the VM under that default path
      (``resolveVMFolder``), never at the drive root.

    An empty ``destination`` means the host's default VM path."""
    clustered = host_is_clustered(host)
    hyperv = (host.hardware or {}).get("hyperv") or {}
    default_path = _norm_path(hyperv.get("defaultVmPath"))
    dest = _norm_path(destination) or default_path
    if clustered:
        if not _is_csv_path(dest):
            raise ApiError(
                "STORAGE_NOT_ALLOWED",
                f"Host '{host.name}' is in a cluster - VMs must be placed on a "
                f"Cluster Shared Volume, not '{dest or 'the default VM path'}'",
            )
        return
    if dest and _is_system_drive(dest) and not _is_system_drive(default_path):
        raise ApiError(
            "STORAGE_NOT_ALLOWED",
            f"VMs cannot be placed on the system drive ('{dest}') - it is not "
            "the host's Hyper-V default VM path",
        )


# action -> (agent function, optimistic transitional state or None when the
# action does not change VM power state)
ACTION_MAP: dict[str, tuple[str, str | None]] = {
    "start": ("vm_start", "Starting"),
    "stop": ("vm_stop", "Stopping"),
    "shutdown": ("vm_shutdown", "Stopping"),
    "restart": ("vm_restart", "Restarting"),
    "pause": ("vm_pause", "Pausing"),
    "delete": ("vm_delete", "Deleting"),
    "enable_metrics": ("vm_enable_metrics", None),
    "disable_metrics": ("vm_disable_metrics", None),
    # --- management actions -------------------------------------------------
    # These are stubs on the agent side for now: the backend queues the task and
    # publishes the AgentRequest, and the worker's timeout sweeper flips it to
    # "timeout" until the Go agent learns to handle the `function`.
    "rename": ("vm_rename", None),
    "edit": ("vm_edit", None),
    "migrate": ("vm_migrate", None),
    "move_storage": ("vm_move", None),
    "startup_change": ("vm_startup_change", None),
    "mount_dvd": ("mount_dvd", None),
    "eject_dvd": ("eject_dvd", None),
    "enable_ha": ("enable_ha", None),
    "disable_ha": ("disable_ha", None),
    "export_template": ("vm_export_template", None),
    "notes_edit": ("notes_edit", None),
    "snapshot_create": ("snapshot_create", None),
    "snapshot_remove": ("snapshot_remove", None),
    "snapshot_restore": ("snapshot_restore", None),
    # read-only: force the agent to re-inventory just this VM ("force refresh")
    "refresh": ("refresh_status", None),
}

# Actions that only read VM state - queued without the VM lock so they neither
# block nor are blocked by a mutating task in flight.
_READ_ONLY_ACTIONS = frozenset({"refresh"})


async def count_host_vms(db: AsyncSession, host_id: str) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(Vm).where(Vm.host_id == host_id)
        )
    ).scalar_one()


async def _queue_vm_task(
    db: AsyncSession,
    vm: Vm,
    *,
    function: str,
    params: dict[str, Any],
    requested_by: str,
    transitional: str | None = None,
    lock: bool = True,
) -> Task:
    if not vm.vm_uuid:
        raise ApiError(
            "VM_NOT_READY",
            "This VM has not been reported by its host agent yet",
            status_code=409,
        )
    host = await db.get(Host, vm.host_id)
    assert host is not None
    _require_host_online(host)

    task_id = new_id()
    # Read-only tasks (a forced inventory refresh) don't take the VM lock: they
    # must not block - nor be blocked by - a mutating operation in flight.
    if lock and not await acquire_vm_lock(
        vm.id, task_id=task_id, kind=function, requested_by=requested_by
    ):
        held = await get_vm_lock(vm.id) or {}
        raise vm_locked(vm.name, held)

    try:
        task = Task(
            id=task_id,
            kind=function,
            status="queued",
            target_type="vm",
            target_id=vm.id,
            target_name=vm.name,
            host_id=vm.host_id,
            requested_by=requested_by,
            progress=0,
        )
        task.correlation_id = task.id
        db.add(task)

        # optimistic state, both in the DB row and the fast cache (only for
        # actions that actually change VM power state)
        if transitional:
            vm.state = transitional
        await db.flush()
        if transitional:
            await set_vm_state(vm.id, transitional)

        request = AgentRequest(
            id=task.id,
            function=function,
            # the agent addresses VMs by their Hyper-V GUID; caller params
            # must never shadow vm_id
            params={**params, "vm_id": vm.vm_uuid},
            requested_by=requested_by,
        )
        task.request_payload = request.model_dump(mode="json")
        await publish_request(host.short_id, request)
    except Exception:
        if lock:
            await release_vm_lock(vm.id, task_id)
        raise
    log.info("queued %s task=%s vm=%s (%s)", function, task.id, vm.id, vm.vm_uuid)
    return task


async def request_vm_action(
    db: AsyncSession,
    vm: Vm,
    action: str,
    *,
    requested_by: str,
    params: dict[str, Any] | None = None,
) -> Task:
    function, transitional = ACTION_MAP[action]
    if action == "migrate" and not vm.highly_available:
        # only cluster roles can move between nodes (Quick/Live via the
        # cluster); a non-HA VM would need a standalone live migration
        raise ApiError(
            "HA_REQUIRED",
            f"'{vm.name}' does not have High Availability enabled - enable HA "
            "before migrating it to another cluster node",
            status_code=409,
        )
    if action == "move_storage":
        dest = _norm_path((params or {}).get("destination_storage"))
        if not dest:
            raise ApiError(
                "VALIDATION_ERROR", "destination_storage is required"
            )
        host = await db.get(Host, vm.host_id)
        assert host is not None
        _require_vm_storage_allowed(host, dest)
    return await _queue_vm_task(
        db,
        vm,
        function=function,
        params=params or {},
        requested_by=requested_by,
        transitional=transitional,
        lock=action not in _READ_ONLY_ACTIONS,
    )




async def request_vm_create(
    db: AsyncSession, host: Host, body: VmCreate, *, requested_by: str
) -> tuple[Vm, Task]:
    """Provision a brand-new VM on ``host``.

    Inserts a placeholder :class:`Vm` row (no ``vm_uuid`` - the agent assigns the
    Hyper-V GUID and reports it back via ``apply_response``), queues a
    ``vm_create`` task and publishes the request to the host agent.
    """
    # Just the target volume/CSV - the agent resolves the per-VM folder
    # (`<base>\<name>` + its Snapshots / Virtual Hard Disks / Virtual Machines
    # subfolders). Empty → the host's default Hyper-V VM path.
    _require_host_online(host)
    destination_storage = (body.destination_storage or "").strip()
    _require_vm_storage_allowed(host, destination_storage)
    memory_bytes = body.memory_mb * 1024 * 1024

    vm = Vm(
        id=new_id(),
        vm_uuid=None,
        host_id=host.id,
        cluster_id=host.cluster_id,
        name=body.name,
        state="Unknown",
        firmware=body.firmware,
        vcpu=body.cpu_count,
        memory_bytes=memory_bytes,
        memory_dynamic=body.memory_dynamic,
        nested_virtualization=body.nested_virtualization,
        highly_available=body.ha_enabled,
        notes=(body.notes or "").strip() or None,
        metrics_enabled=False,
        # brand-new row: initialise the selectin relationships so the synchronous
        # serializer (vm_out) never triggers a lazy load → MissingGreenlet
        disks=[],
        nics=[],
        snapshots=[],
    )
    db.add(vm)
    if body.vlan_id is not None:
        vm.nics.append(
            VmNic(
                id=new_id(),
                position=0,
                name="Network Adapter",
                vlan_id=body.vlan_id,
                ip_addresses=[],
                connected=True,
            )
        )
    await db.flush()

    task_id = new_id()
    # a fresh VM can't already be locked, but keep the invariant "a mutating
    # task ⇒ the VM is locked" so nothing races the create either.
    await acquire_vm_lock(
        vm.id, task_id=task_id, kind="vm_create", requested_by=requested_by
    )
    task = Task(
        id=task_id,
        kind="vm_create",
        status="queued",
        target_type="vm",
        target_id=vm.id,
        target_name=vm.name,
        host_id=host.id,
        requested_by=requested_by,
        progress=0,
    )
    task.correlation_id = task.id
    db.add(task)

    request = AgentRequest(
        id=task.id,
        function="vm_create",
        params={
            "name": body.name,
            "destination_storage": destination_storage,
            "os": body.os,
            "notes": vm.notes or "",
            "firmware": body.firmware,
            "cpu_count": body.cpu_count,
            "memory_mb": body.memory_mb,
            "memory_dynamic": body.memory_dynamic,
            "memory_min_mb": body.memory_min_mb,
            "memory_max_mb": body.memory_max_mb,
            "nested_virtualization": body.nested_virtualization,
            "vlan_id": body.vlan_id,
            "switch_name": body.switch_name or None,
            "dvd": body.dvd or None,
            "ha_enabled": body.ha_enabled and host.cluster_id is not None,
            "cluster": host.cluster_id if body.ha_enabled else None,
            "start_now": body.start_now,
            "disks": [
                {"name": d.name, "type": d.type, "size_gb": d.size_gb}
                for d in body.disks
            ],
        },
        requested_by=requested_by,
    )
    task.request_payload = request.model_dump(mode="json")
    await db.flush()

    await publish_request(host.short_id, request)
    log.info("queued vm_create task=%s vm=%s host=%s", task.id, vm.id, host.id)
    return vm, task


async def remove_vm_from_inventory(db: AsyncSession, vm: Vm) -> None:
    """Drop a VM row (and its disks/nics/snapshots) from the database only - no
    agent request. This is the escape hatch for a VM stranded ``Unknown`` on a
    host that is gone for good.

    Refused while the host agent is still online and the VM has been reported by
    it: deleting the row then would just have the next ``vm_inventory`` recreate
    it. A never-reported placeholder (no ``vm_uuid``) can always be removed.
    """
    host = await db.get(Host, vm.host_id)
    if host is not None and vm.vm_uuid is not None and agent_connected(host):
        raise ApiError(
            "HOST_ONLINE",
            f"Host '{host.name}' is online - use Delete to remove the VM, or it "
            "will reappear on the next inventory refresh",
            status_code=409,
        )
    await release_vm_lock(vm.id)
    await clear_vm_state(vm.id)
    await db.delete(vm)
    await db.flush()
    log.info("removed vm=%s (%s) from inventory", vm.id, vm.vm_uuid)


async def request_vm_clone(
    db: AsyncSession, body: VmClone, host: Host, *, requested_by: str
) -> tuple[Vm, Task]:
    """Clone an off VM (``clone_type="imported"``) or deploy an exported template
    (``clone_type="exported"``) onto ``host``.

    Same placeholder lifecycle as :func:`request_vm_create`: a ``Vm`` row is
    inserted now (no ``vm_uuid``); ``apply_response`` stamps the GUID + state on
    success or drops the row on failure (``_apply_response_state`` handles
    ``vm_clone`` alongside ``vm_create``).
    """
    _require_host_online(host)
    _require_vm_storage_allowed(host, body.destination_storage)

    firmware = "UEFI"
    cpu_count = body.cpu_count
    memory_mb = body.memory_mb
    vlan_id = body.vlan_id
    notes = (body.notes or "").strip() or None
    clone_params: dict[str, Any] = {}

    if body.source == "vm":
        source_vm = (
            await db.execute(
                select(Vm)
                .where(Vm.id == body.source_vm_id)
                .options(selectinload(Vm.nics))
            )
        ).scalar_one_or_none()
        if source_vm is None:
            raise ApiError("NOT_FOUND", "Source VM not found", status_code=404)
        if source_vm.host_id != host.id:
            raise ApiError(
                "CROSS_HOST_CLONE",
                "The source VM and the target host must be the same host",
                status_code=409,
            )
        if source_vm.state != "Off":
            raise ApiError(
                "SOURCE_NOT_OFF",
                f"'{source_vm.name}' must be powered off to clone it",
                status_code=409,
            )
        if not source_vm.vm_uuid:
            raise ApiError(
                "VM_NOT_READY",
                "The source VM has not been reported by its host agent yet",
                status_code=409,
            )
        firmware = source_vm.firmware or firmware
        cpu_count = cpu_count or source_vm.vcpu
        memory_mb = memory_mb or (source_vm.memory_bytes // (1024 * 1024))
        if vlan_id is None and source_vm.nics:
            vlan_id = source_vm.nics[0].vlan_id
        if notes is None:
            notes = source_vm.notes
        clone_params = {
            "clone_type": "imported",
            "source_vm_name": source_vm.name,
        }
    else:
        template = await db.get(Template, body.template_id)
        if template is None:
            raise ApiError("NOT_FOUND", "Template not found", status_code=404)
        tpl_host = await db.get(Host, template.host_id)
        same_cluster = (
            tpl_host is not None
            and host.cluster_id is not None
            and tpl_host.cluster_id == host.cluster_id
        )
        if template.host_id != host.id and not same_cluster:
            raise ApiError(
                "TEMPLATE_UNREACHABLE",
                "The template is not on the target host or its cluster",
                status_code=409,
            )
        cpu_count = cpu_count or template.cpu_count or 2
        memory_mb = memory_mb or template.memory_mb or 4096
        if notes is None:
            notes = template.notes
        clone_params = {
            "clone_type": "exported",
            "source_export_path": template.path,
            "expand_disks": body.expand_disks,
        }

    vm = Vm(
        id=new_id(),
        vm_uuid=None,
        host_id=host.id,
        cluster_id=host.cluster_id,
        name=body.name,
        state="Unknown",
        firmware=firmware,
        vcpu=cpu_count or 2,
        memory_bytes=(memory_mb or 4096) * 1024 * 1024,
        memory_dynamic=False,
        nested_virtualization=body.nested_virtualization,
        highly_available=body.ha_enabled and host.cluster_id is not None,
        notes=notes,
        metrics_enabled=False,
        disks=[],
        nics=[],
        snapshots=[],
    )
    db.add(vm)
    if vlan_id is not None:
        vm.nics.append(
            VmNic(
                id=new_id(),
                position=0,
                name="Network Adapter",
                vlan_id=vlan_id,
                ip_addresses=[],
                connected=True,
            )
        )
    await db.flush()

    task_id = new_id()
    await acquire_vm_lock(
        vm.id, task_id=task_id, kind="vm_clone", requested_by=requested_by
    )
    task = Task(
        id=task_id,
        kind="vm_clone",
        status="queued",
        target_type="vm",
        target_id=vm.id,
        target_name=vm.name,
        host_id=host.id,
        requested_by=requested_by,
        progress=0,
    )
    task.correlation_id = task.id
    db.add(task)

    request = AgentRequest(
        id=task.id,
        function="vm_clone",
        params={
            **clone_params,
            "name": body.name,
            "destination_storage": (body.destination_storage or "").strip(),
            "cpu_count": cpu_count or 2,
            "memory_mb": memory_mb or 4096,
            "notes": notes or "",
            "vlan_id": vlan_id,
            "nested_virtualization": body.nested_virtualization,
            "ha_enabled": body.ha_enabled and host.cluster_id is not None,
            "start_now": body.start_now,
        },
        requested_by=requested_by,
    )
    task.request_payload = request.model_dump(mode="json")
    await db.flush()

    await publish_request(host.short_id, request)
    log.info("queued vm_clone task=%s vm=%s host=%s", task.id, vm.id, host.id)
    return vm, task

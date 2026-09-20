from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from ..config import get_settings
from ..models import (
    Folder,
    Host,
    HostMetric,
    Iso,
    Task,
    Template,
    Vlan,
    Vm,
    VmMetric,
)
from ..models import VmDisk as VmDiskRow
from ..models import VmNic as VmNicRow
from ..models import VmSnapshot as VmSnapshotRow
from ..schemas import (
    FolderOut,
    HostAgentStatus,
    HostDetailOut,
    HostHardwareInventory,
    HostMetricSample,
    HostOut,
    IsoOut,
    TaskDetailOut,
    TaskOut,
    TemplateOut,
    VlanOut,
    VmDisk,
    VmLock,
    VmMemory,
    VmMetricSample,
    VmNic,
    VmOut,
    VmSnapshot,
)
from ..schemas.host import RefreshIntervals
from .inventory import normalize_hardware

log = logging.getLogger(__name__)
_settings = get_settings()


def _hardware_out(raw: dict | None) -> HostHardwareInventory | None:
    """Validate stored hardware into the API shape. `normalize_hardware` maps a
    legacy (pre-alignment) row and passes a canonical one through untouched;
    never let a bad row 500 the host endpoint."""
    canonical = normalize_hardware(raw)
    if canonical is None:
        return None
    try:
        return HostHardwareInventory.model_validate(canonical)
    except ValidationError:
        log.warning(
            "unparseable hardware inventory (dropped): keys=%s", sorted(canonical)[:12]
        )
        return None


def agent_connected(host: Host) -> bool:
    """True while the host agent is still checking in - its last agent_status is
    newer than ``agent_offline_after_seconds``. When it flips to False the host
    reads as offline and every VM it owns is reported as ``Unknown``."""
    if host.agent_last_seen is None:
        return False
    last = host.agent_last_seen
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    cutoff = datetime.now(UTC) - timedelta(
        seconds=_settings.agent_offline_after_seconds
    )
    return last >= cutoff


# kept as a private alias for existing imports/tests
_agent_connected = agent_connected


def agent_status(host: Host) -> HostAgentStatus:
    connected = agent_connected(host)
    intervals = None
    if host.agent_refresh_vm is not None and host.agent_refresh_host is not None:
        intervals = RefreshIntervals(
            vm=host.agent_refresh_vm, host=host.agent_refresh_host
        )
    return HostAgentStatus(
        version=host.agent_version,
        last_seen=host.agent_last_seen,
        connected=connected,
        refresh_intervals=intervals,
    )


def host_out(host: Host, vm_count: int) -> HostOut:
    return HostOut(
        id=host.id,
        short_id=host.short_id,
        cluster_id=host.cluster_id,
        name=host.name,
        fqdn=host.fqdn,
        ip_address=host.ip_address,
        hypervisor=host.hypervisor,
        online=agent_connected(host),
        agent=agent_status(host),
        vm_count=vm_count,
    )


def host_detail_out(host: Host, vm_count: int) -> HostDetailOut:
    hardware = _hardware_out(host.hardware)
    return HostDetailOut(
        id=host.id,
        short_id=host.short_id,
        cluster_id=host.cluster_id,
        name=host.name,
        fqdn=host.fqdn,
        ip_address=host.ip_address,
        hypervisor=host.hypervisor,
        online=agent_connected(host),
        agent=agent_status(host),
        vm_count=vm_count,
        hardware=hardware,
    )


def _disk_out(d: VmDiskRow) -> VmDisk:
    return VmDisk(
        id=d.disk_id or "",
        path=d.path or "",
        controller=d.controller or "",
        size_bytes=d.size_bytes or 0,
        used_bytes=d.used_bytes,
        type=d.type or "",
        format=d.format or "",
    )


def _nic_out(n: VmNicRow) -> VmNic:
    return VmNic(
        id=n.nic_id or "",
        name=n.name or "",
        switch_name=n.switch_name or "",
        vlan_id=n.vlan_id,
        mac_address=n.mac_address or "",
        ip_addresses=list(n.ip_addresses or []),
        connected=n.connected,
    )


def _snapshot_out(s: VmSnapshotRow) -> VmSnapshot:
    return VmSnapshot(
        id=s.snapshot_id or "",
        name=s.name or "",
        created_at=s.snapshot_created_at,
        parent_id=s.parent_snapshot_id,
        type=s.snapshot_type,
    )


def vm_out(
    vm: Vm,
    state_override: str | None = None,
    lock: dict | None = None,
    *,
    host_offline: bool = False,
) -> VmOut:
    # A host whose agent stopped checking in can no longer vouch for any of its
    # VMs - report them all as Unknown (and ignore the fast-cache overlay), which
    # is also what gates VM actions in the API and the UI.
    state = "Unknown" if host_offline else (state_override or vm.state)
    return VmOut(
        id=vm.id,
        vm_uuid=vm.vm_uuid,
        host_id=vm.host_id,
        folder_id=vm.folder_id,
        name=vm.name,
        state=state,
        last_seen=vm.last_inventory_at,
        firmware=vm.firmware or "UEFI",
        uptime_sec=vm.uptime_sec,
        vcpu=vm.vcpu,
        cpu_usage_percent=vm.cpu_usage_percent,
        memory=VmMemory(
            assigned_bytes=vm.memory_bytes or 0,
            min_bytes=vm.memory_min_bytes,
            max_bytes=vm.memory_max_bytes,
            dynamic=vm.memory_dynamic,
            demand_bytes=vm.memory_demand_bytes,
        ),
        disks=[_disk_out(d) for d in vm.disks],
        nics=[_nic_out(n) for n in vm.nics],
        snapshots=[_snapshot_out(s) for s in vm.snapshots],
        secure_boot=vm.secure_boot,
        secure_boot_template=vm.secure_boot_template,
        nested_virtualization=vm.nested_virtualization,
        auto_start_action=vm.auto_start_action,
        auto_start_delay_sec=vm.auto_start_delay_sec,
        auto_stop_action=vm.auto_stop_action,
        config_path=vm.config_path,
        dvd_path=vm.dvd_path,
        highly_available=vm.highly_available,
        notes=vm.notes,
        metrics_enabled=vm.metrics_enabled,
        created_at=vm.vm_created_at,
        lock=VmLock.model_validate(lock) if lock else None,
    )


def host_metric_out(row: HostMetric) -> HostMetricSample:
    return HostMetricSample.model_validate(row)


def vm_metric_out(row: VmMetric) -> VmMetricSample:
    return VmMetricSample.model_validate(row)


def folder_out(folder: Folder) -> FolderOut:
    return FolderOut(
        id=folder.id,
        name=folder.name,
        cluster_id=folder.cluster_id,
        host_id=folder.host_id,
    )


def vlan_out(vlan: Vlan) -> VlanOut:
    return VlanOut(
        id=vlan.id,
        name=vlan.name,
        vlan_id=vlan.vlan_id,
        description=vlan.description,
        is_default=vlan.is_default,
        cluster_id=vlan.cluster_id,
        host_id=vlan.host_id,
    )


def template_out(t: Template) -> TemplateOut:
    return TemplateOut(
        id=t.id,
        host_id=t.host_id,
        name=t.name,
        path=t.path,
        size_bytes=t.size_bytes,
        disk_size_bytes=t.disk_size_bytes,
        notes=t.notes,
        cpu_count=t.cpu_count,
        memory_mb=t.memory_mb,
        guest_os=t.guest_os,
        created_at=t.image_created_at,
    )


def iso_out(i: Iso) -> IsoOut:
    return IsoOut(
        id=i.id,
        host_id=i.host_id,
        name=i.name,
        path=i.path,
        size_bytes=i.size_bytes,
        checksum=i.checksum,
    )


def task_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        kind=task.kind,
        status=task.status,
        target_type=task.target_type,
        target_id=task.target_id,
        target_name=task.target_name,
        requested_by=task.requested_by,
        progress=task.progress,
        progress_message=task.progress_message,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        result=task.result,
        error=task.error,
        correlation_id=task.correlation_id,
    )


def task_detail_out(task: Task) -> TaskDetailOut:
    return TaskDetailOut(
        **task_out(task).model_dump(),
        request_payload=task.request_payload,
        response_payload=task.response_payload,
    )

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import CamelModel
from .task import TaskOut


class VmMemory(CamelModel):
    assigned_bytes: int
    min_bytes: int | None = None
    max_bytes: int | None = None
    dynamic: bool = False
    # current guest memory demand - a simple hypervisor average, refreshed each
    # inventory (not a time-series; see vm_metrics for history)
    demand_bytes: int | None = None


class VmDisk(CamelModel):
    id: str = ""
    path: str = ""
    controller: str = ""
    size_bytes: int = 0
    used_bytes: int | None = None
    # provisioning: "Fixed" | "Dynamic" | "Differencing"
    type: str = ""
    # container format: "VHDX" | "VHD" | "passthrough"
    format: str = ""


class VmNic(CamelModel):
    id: str = ""
    name: str = ""
    switch_name: str = ""
    vlan_id: int | None = None
    mac_address: str = ""
    ip_addresses: list[str] = []
    connected: bool = True


class VmMove(CamelModel):
    """PATCH /vms/{id} - folderId of a reachable folder, or null to remove."""

    folder_id: str | None = None


class VmActionRequest(CamelModel):
    """Optional body for POST /vms/{id}/actions/{action} and /clone.
    `params` is a free-form param bag forwarded to the agent as
    AgentRequest.params (the agent contract defines the keys per action)."""

    params: dict[str, Any] = {}


class VmDiskSpec(CamelModel):
    """One virtual disk to create with a new VM."""

    name: str = Field(min_length=1, max_length=6, pattern=r"^[a-zA-Z0-9]+$")
    type: Literal["Fixed", "Dynamic"] = "Fixed"
    size_gb: int = Field(default=60, ge=1, le=65536)


class VmCreate(CamelModel):
    """POST /vms - provision a brand-new VM on a host.

    The backend inserts a placeholder ``Vm`` row (no ``vmUuid`` yet), queues a
    ``vm_create`` task and publishes it to the host agent. ``apply_response``
    stamps the real Hyper-V GUID + state onto the row when the agent finishes,
    or deletes the placeholder if it fails.
    """

    name: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$")
    host_id: str
    os: Literal["windows", "linux", "other"] = "windows"
    firmware: Literal["BIOS", "UEFI"] = "UEFI"
    cpu_count: int = Field(default=2, ge=1, le=256)
    # startup memory. With memory_dynamic it is the "Startup RAM"; the guest can
    # then balloon between memory_min_mb and memory_max_mb.
    memory_mb: int = Field(default=4096, ge=256, le=4194304)
    memory_dynamic: bool = False
    # only used when memory_dynamic; the agent fills sane defaults when omitted
    memory_min_mb: int | None = Field(default=None, ge=256, le=4194304)
    memory_max_mb: int | None = Field(default=None, ge=256, le=4194304)
    # Volume / CSV the VM should live on (e.g. "E:\\", "C:\\ClusterStorage\\Volume2").
    # Empty / omitted → the host's default Hyper-V VM path. The agent resolves the
    # actual per-VM folder (`<base>\\<name>` with its subfolders); the backend never
    # sends a fully-qualified path.
    destination_storage: str | None = None
    notes: str | None = None
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    switch_name: str | None = None
    nested_virtualization: bool = False
    ha_enabled: bool = False
    dvd: str | None = None
    start_now: bool = False
    disks: list[VmDiskSpec] = Field(default_factory=lambda: [VmDiskSpec(name="OS")])

    @model_validator(mode="after")
    def _check_dynamic_memory(self) -> VmCreate:
        if not self.memory_dynamic:
            return self
        lo = self.memory_min_mb
        hi = self.memory_max_mb
        if lo is not None and lo > self.memory_mb:
            raise ValueError("memory_min_mb cannot exceed the startup memory_mb")
        if hi is not None and hi < self.memory_mb:
            raise ValueError("memory_max_mb cannot be below the startup memory_mb")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("memory_min_mb cannot exceed memory_max_mb")
        return self


class VmClone(CamelModel):
    """POST /vms/clone - provision a new VM by cloning an off VM or deploying an
    exported template.

    Same placeholder-row lifecycle as ``VmCreate``: the backend inserts a
    ``Vm`` row (no ``vmUuid``), queues a ``vm_clone`` task, and ``apply_response``
    stamps the GUID + state on success or drops the row on failure.

    Exactly one of ``source_vm_id`` (``source="vm"``, must be an *off* VM on the
    target host) / ``template_id`` (``source="template"``) is required. Sizing
    (``cpu_count`` / ``memory_mb`` / ``vlan_id`` / ``notes``) defaults to the
    source's when omitted. Disks, firmware and NICs are inherited from the source.
    """

    name: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$")
    host_id: str
    source: Literal["vm", "template"]
    source_vm_id: str | None = None
    template_id: str | None = None
    cpu_count: int | None = Field(default=None, ge=1, le=256)
    memory_mb: int | None = Field(default=None, ge=256, le=4194304)
    destination_storage: str | None = None
    notes: str | None = None
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    nested_virtualization: bool = False
    ha_enabled: bool = False
    start_now: bool = False
    # template deploys only: convert the deployed VM's Dynamic disks to Fixed
    # after import. On by default; set false to keep the template's Dynamic disks.
    expand_disks: bool = True

    @model_validator(mode="after")
    def _check_source(self) -> VmClone:
        if self.source == "vm" and not self.source_vm_id:
            raise ValueError("source_vm_id is required when source='vm'")
        if self.source == "template" and not self.template_id:
            raise ValueError("template_id is required when source='template'")
        return self


class VmLock(CamelModel):
    """Present while a mutating agent task is running on the VM - the UI blocks
    further operations until it clears."""

    task_id: str
    # the agent function holding the lock, e.g. "vm_edit", "vm_start"
    kind: str
    requested_by: str
    acquired_at: datetime


class VmLockEntry(VmLock):
    """One row of the admin `GET /vm-locks` view."""

    vm_id: str
    vm_name: str | None = None
    # seconds until the lock's safety-net TTL expires
    ttl: int
    # the backing task's current status, when it still exists
    task_status: str | None = None


class VmSnapshot(CamelModel):
    id: str = ""
    name: str = ""
    created_at: datetime | None = None
    parent_id: str | None = None
    # "Standard" | "Production" | "Recovery" | … - hypervisor-defined
    type: str | None = None


class VmOut(CamelModel):
    id: str
    # the Hyper-V VM GUID; null until the host agent first reports the VM
    vm_uuid: str | None = None
    host_id: str
    folder_id: str | None = None
    name: str
    state: str
    # last time the host agent reported this VM in an inventory refresh; null
    # until the first report. Goes Unknown-stale once the host drops offline.
    last_seen: datetime | None = None
    firmware: str = "UEFI"
    uptime_sec: int | None = None
    vcpu: int
    # simple hypervisor CPU-usage average (percent), refreshed each inventory
    cpu_usage_percent: float | None = None
    memory: VmMemory
    disks: list[VmDisk]
    nics: list[VmNic]
    snapshots: list[VmSnapshot] = []
    # --- boot / firmware ---
    secure_boot: bool | None = None
    secure_boot_template: str | None = None
    nested_virtualization: bool = False
    # --- automatic start/stop policy ---
    auto_start_action: str | None = None
    auto_start_delay_sec: int | None = None
    auto_stop_action: str | None = None
    # --- placement / state ---
    config_path: str | None = None
    dvd_path: str | None = None
    highly_available: bool = False
    notes: str | None = None
    metrics_enabled: bool = False
    created_at: datetime | None = None
    # non-null while an operation is running on the VM (see VmLock)
    lock: VmLock | None = None


class VmCreateEnvelope(CamelModel):
    """201 response body for POST /vms - the placeholder VM row and its task."""

    vm: VmOut
    task: TaskOut

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..ids import new_id
from .base import UUID_STR, Base, TimestampMixin

# Values mirror the frontend VmState union (docs/api-contract.md).
VM_STATES = (
    "Running",
    "Off",
    "Paused",
    "Saved",
    "Starting",
    "Stopping",
    "Saving",
    "Pausing",
    "Resuming",
    "Restarting",
    "Deleting",
    "Unknown",
)


class Vm(Base, TimestampMixin):
    __tablename__ = "vms"

    # Backend-generated. NOT the Hyper-V VM GUID - the same GUID can legitimately
    # exist on more than one host (a clone, an exported-then-imported VM), so it
    # cannot be the PK. See `vm_uuid` below.
    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)

    # The Hyper-V VM GUID as reported by the agent (`$vm.Id.Guid`). Unique within
    # a cluster (or within a standalone host) - enforced by the partial unique
    # indexes in __table_args__ and by the cluster-scoped upsert in
    # services.inventory.apply_vm_inventory. Nullable so a backend-created VM can
    # exist before its first inventory refresh.
    vm_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    host_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalised from the owning host so the "unique per cluster" rule is
    # DB-enforceable and the migration lookup stays a single indexed query.
    # Kept in sync by apply_vm_inventory and services.organization.move_host.
    cluster_id: Mapped[str | None] = mapped_column(
        UUID_STR,
        ForeignKey("clusters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    folder_id: Mapped[str | None] = mapped_column(
        UUID_STR,
        ForeignKey("folders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="Unknown")
    # Virtualizer-neutral boot firmware: "BIOS" (Hyper-V gen 1) or "UEFI" (gen 2).
    firmware: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="UEFI"
    )
    uptime_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vcpu: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- memory (bytes; flat columns so callers don't parse a JSON blob) -----
    # configured/startup memory
    memory_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # dynamic-memory floor / ceiling (null when the hypervisor doesn't report them)
    memory_min_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    memory_max_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    memory_dynamic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # --- live-ish counters the hypervisor keeps cheaply (a simple average, not a
    # time-series - the rolling history lives in vm_metrics and only for metered
    # VMs). Refreshed on every vm_inventory. ----------------------------------
    cpu_usage_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_demand_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- boot / firmware ----------------------------------------------------
    secure_boot: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # "Windows" | "Linux" | "Others" | raw template name
    secure_boot_template: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nested_virtualization: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # --- automatic start/stop policy --------------------------------------
    # start: "Nothing" | "StartIfRunning" | "Start"
    auto_start_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    auto_start_delay_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # stop: "TurnOff" | "Save" | "ShutDown"
    auto_stop_action: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- misc placement / state ------------------------------------------
    # the hypervisor-side folder that holds the VM's config files
    config_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # currently-mounted ISO/DVD image path, when any
    dvd_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # part of a HA / failover cluster resource group
    highly_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # whether hypervisor resource metering is enabled on this VM (agent-reported
    # via vm_inventory). Gates whether the VM shows up in <hostid>.vm_metrics.
    metrics_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # VM creation time reported by the agent (contract field `createdAt`)
    vm_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # bookkeeping: last time an inventory refreshed this row
    last_inventory_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    disks: Mapped[list[VmDisk]] = relationship(
        "VmDisk",
        back_populates="vm",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="VmDisk.position",
    )
    nics: Mapped[list[VmNic]] = relationship(
        "VmNic",
        back_populates="vm",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="VmNic.position",
    )
    snapshots: Mapped[list[VmSnapshot]] = relationship(
        "VmSnapshot",
        back_populates="vm",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="VmSnapshot.position",
    )

    __table_args__ = (
        # one row per VM GUID inside a cluster …
        Index(
            "uq_vms_cluster_vm_uuid",
            "cluster_id",
            "vm_uuid",
            unique=True,
            postgresql_where=text("cluster_id IS NOT NULL AND vm_uuid IS NOT NULL"),
        ),
        # … or inside a standalone host
        Index(
            "uq_vms_host_vm_uuid",
            "host_id",
            "vm_uuid",
            unique=True,
            postgresql_where=text("cluster_id IS NULL AND vm_uuid IS NOT NULL"),
        ),
    )


# vm_disks / vm_nics / vm_snapshots are rebuilt wholesale on every inventory
# refresh (services.inventory._sync_*). Two handlers for the same host can
# interleave (a periodic vm_inventory + a task response's vm_status), and a VM
# row that disappears takes its children with it via ON DELETE CASCADE - so a
# child scheduled for delete-orphan may already be gone. That is benign for a
# wholesale replace: confirm_deleted_rows=False silences the "expected to delete
# 1 row(s); 0 were matched" warning for exactly this pattern.
_WHOLESALE_REPLACE = {"confirm_deleted_rows": False}


class VmDisk(Base):
    """A virtual disk attached to a VM. Rebuilt from vm_inventory each refresh
    (see services.inventory._sync_disks); `position` keeps a stable order."""

    __tablename__ = "vm_disks"
    __mapper_args__ = _WHOLESALE_REPLACE

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    vm_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("vms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # the agent's disk handle (contract `disk.id`, e.g. "scsi-0-0")
    disk_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    controller: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # provisioning type: "Fixed" | "Dynamic" | "Differencing"
    type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # container format: "VHDX" | "VHD" | "passthrough"
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)

    vm: Mapped[Vm] = relationship("Vm", back_populates="disks")


class VmSnapshot(Base):
    """A checkpoint/snapshot of a VM. Rebuilt from vm_inventory each refresh
    (see services.inventory._sync_snapshots); `position` keeps a stable order."""

    __tablename__ = "vm_snapshots"
    __mapper_args__ = _WHOLESALE_REPLACE

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    vm_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("vms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # the hypervisor's own snapshot id
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # snapshot id of the parent checkpoint, when this one is nested
    parent_snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # e.g. "Standard" | "Production" | "Recovery" - hypervisor-defined
    snapshot_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    vm: Mapped[Vm] = relationship("Vm", back_populates="snapshots")


class VmNic(Base):
    """A network adapter attached to a VM. Rebuilt from vm_inventory each refresh
    (see services.inventory._sync_nics); `position` keeps a stable order."""

    __tablename__ = "vm_nics"
    __mapper_args__ = _WHOLESALE_REPLACE

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    vm_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("vms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # the agent's adapter handle (contract `nic.id`)
    nic_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    switch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 802.1Q access VLAN tag on this adapter, when the agent reports one.
    vlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mac_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_addresses: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    connected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    vm: Mapped[Vm] = relationship("Vm", back_populates="nics")

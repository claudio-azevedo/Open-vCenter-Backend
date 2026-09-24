from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..cache import (
    clear_vm_deleted,
    clear_vm_state,
    list_deleted_vm_uuids,
    mark_vm_deleted,
    release_vm_lock,
    set_vm_state,
)
from ..config import get_settings
from ..ids import new_id
from ..messaging import AgentResponse
from ..models import (
    Host,
    HostMetric,
    Iso,
    Task,
    Template,
    Vm,
    VmDisk,
    VmMetric,
    VmNic,
    VmSnapshot,
)

log = logging.getLogger(__name__)

_MIB = 1024**2
_GIB = 1024**3


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


async def _host_label(db: AsyncSession, host_id: str) -> str:
    """`host=<name> id=<short_id>` for logs - falls back to the UUID if unknown."""
    host = await db.get(Host, host_id)
    if host is None:
        return f"host=? id={host_id}"
    return f"host={host.name} id={host.short_id}"


# ---- agent_status -----------------------------------------------------------

async def apply_agent_status(db: AsyncSession, host_id: str, payload: dict) -> None:
    host = await db.get(Host, host_id)
    if host is None:
        log.warning("agent_status for unknown host=%s", host_id)
        return
    host.agent_version = payload.get("version")
    host.agent_refresh_vm = payload.get("vm_refresh_interval") or payload.get(
        "vmRefreshInterval"
    )
    host.agent_refresh_host = payload.get("host_refresh_interval") or payload.get(
        "hostRefreshInterval"
    )
    if (hv := payload.get("hypervisor")):
        host.hypervisor = hv
    # fqdn / primary IP are agent-resolved (not asked for at registration)
    if (fqdn := payload.get("fqdn") or payload.get("hostname")):
        host.fqdn = fqdn
    if (ip := payload.get("ip") or payload.get("ipAddress") or payload.get("primaryIp")):
        host.ip_address = ip
    host.agent_last_seen = _parse_dt(payload.get("reported_at")) or _now()


# ---- vm_inventory ---------------------------------------------------------

def _split_ips(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        return [p for p in re.split(r"[,;\s]+", value) if p]
    return []


def _firmware_of(item: dict) -> str:
    """Virtualizer-neutral boot firmware. Prefer the agent's `firmware`
    ("BIOS"/"UEFI"); fall back to a legacy Hyper-V `generation` int (1 -> BIOS)."""
    fw = str(item.get("firmware") or "").upper()
    if fw in ("BIOS", "UEFI"):
        return fw
    return "BIOS" if item.get("generation") == 1 else "UEFI"


def _mib_to_bytes(value) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value) * _MIB))
    except (TypeError, ValueError):
        return None


def _first(item: dict, *keys):
    """First present (not-None) value among keys."""
    for k in keys:
        if item.get(k) is not None:
            return item[k]
    return None


def normalize_vm(item: dict) -> dict:
    """Map the agent's `VMInfo` onto the backend `Vm` shape (schemas/vm.py).
    Idempotent - fields already in canonical form pass through."""
    # vcpu: agent sends {cpu:{vcpus}}, canonical is a flat int
    vcpu = item.get("vcpu")
    if vcpu is None and isinstance(item.get("cpu"), dict):
        vcpu = item["cpu"].get("vcpus")

    # memory: flat byte columns. Agent sends memoryMb (int, MiB) + min/max in
    # bytes; a canonical {assignedBytes, minBytes, maxBytes, dynamic} object also
    # passes through (idempotent re-normalize / fake agent).
    mem_obj = item.get("memory") if isinstance(item.get("memory"), dict) else {}
    memory_bytes = _first(item, "memoryBytes") or mem_obj.get("assignedBytes")
    if memory_bytes is None:
        memory_bytes = _mib_to_bytes(_first(item, "memoryMb", "memoryMB")) or 0
    memory_min = _first(item, "memoryMinBytes") or mem_obj.get("minBytes")
    memory_max = _first(item, "memoryMaxBytes") or mem_obj.get("maxBytes")
    memory_dynamic = bool(item.get("memoryDynamic") or mem_obj.get("dynamic"))
    memory_demand = _first(item, "memoryDemandBytes")
    if memory_demand is None:
        memory_demand = _mib_to_bytes(_first(item, "memoryConsumedMb"))

    cpu_usage = _first(item, "cpuUsagePercent", "cpuUsagePct")

    # disks: agent sends vhds[{diskId,path,controller,format,type,sizeGb,usedBytes}]
    disks = item.get("disks")
    if disks is None:
        disks = [
            {
                "id": v.get("diskId") or v.get("id") or "",
                "path": v.get("path", ""),
                "controller": v.get("controller", ""),
                "sizeBytes": int(round(float(v.get("sizeGb") or 0) * _GIB)),
                "usedBytes": _as_int(v.get("usedBytes")),
                # "type" is the provisioning kind (Fixed/Dynamic); "format" the
                # container (VHDX/VHD). Keep them distinct.
                "type": v.get("type") or "",
                "format": (v.get("format") or "").upper(),
            }
            for v in (item.get("vhds") or [])
            if isinstance(v, dict)
        ]

    # nics: agent sends {networkId, vlanId, ipAddresses:"a, b"} etc.
    nics = item.get("nics") or []
    if nics and isinstance(nics[0], dict) and "switchName" not in nics[0]:
        nics = [
            {
                "id": n.get("id", ""),
                "name": n.get("name", ""),
                "switchName": n.get("switchName") or n.get("networkId") or "",
                "vlanId": n.get("vlanId"),
                "macAddress": n.get("macAddress", ""),
                "ipAddresses": _split_ips(n.get("ipAddresses")),
                "connected": bool(n.get("connected", True)),
            }
            for n in nics
            if isinstance(n, dict)
        ]

    # snapshots: agent sends [{id,name,creationTime,snapshotType,parentSnapshotId}]
    snapshots = item.get("snapshots")
    if snapshots is None:
        snapshots = []
    snapshots = [
        {
            "id": s.get("id") or s.get("snapshotId") or "",
            "name": s.get("name", ""),
            "parentId": s.get("parentId") or s.get("parentSnapshotId"),
            "type": s.get("type") or s.get("snapshotType") or None,
            "createdAt": s.get("createdAt") or s.get("creationTime"),
        }
        for s in snapshots
        if isinstance(s, dict)
    ]

    dvd = _first(item, "dvdPath", "dvd")

    return {
        # the Hyper-V VM GUID - our Vm.vm_uuid, NOT our PK
        "vmUuid": item.get("id") or item.get("vmId"),
        "name": item.get("name"),
        "state": item.get("state", "Unknown"),
        "metricsEnabled": item.get("metricsEnabled"),
        "firmware": _firmware_of(item),
        "uptimeSec": item.get("uptimeSec"),
        "vcpu": vcpu,
        "memoryBytes": _as_int(memory_bytes) or 0,
        "memoryMinBytes": _as_int(memory_min),
        "memoryMaxBytes": _as_int(memory_max),
        "memoryDynamic": memory_dynamic,
        "memoryDemandBytes": _as_int(memory_demand),
        "cpuUsagePercent": _as_float(cpu_usage),
        "secureBoot": item.get("secureBoot"),
        "secureBootTemplate": item.get("secureBootTemplate") or None,
        "nestedVirtualization": bool(
            _first(item, "nestedVirtualization", "nested") or False
        ),
        "autoStartAction": _first(item, "autoStartAction", "automaticStart"),
        "autoStartDelaySec": _as_int(
            _first(item, "autoStartDelaySec", "automaticStartDelay")
        ),
        "autoStopAction": _first(item, "autoStopAction", "automaticStop"),
        "configPath": _first(item, "configPath", "path"),
        "dvdPath": dvd,
        "highlyAvailable": bool(_first(item, "highlyAvailable", "ha") or False),
        "disks": disks,
        "nics": nics,
        "snapshots": snapshots,
        "notes": item.get("notes"),
        "createdAt": item.get("createdAt") or item.get("creationTime"),
    }


async def _inventory_scope_host_ids(db: AsyncSession, host: Host) -> list[str]:
    """Hosts a VM GUID must be unique across for this inventory: the whole
    cluster if the reporting host is clustered, otherwise just the host itself.
    A VM that live-migrated between cluster nodes stays one row."""
    if host.cluster_id is None:
        return [host.id]
    return list(
        (
            await db.execute(select(Host.id).where(Host.cluster_id == host.cluster_id))
        ).scalars().all()
    )


def _sync_disks(vm: Vm, disks: list[dict]) -> None:
    """Replace vm.disks wholesale from the normalized inventory list. The
    delete-orphan cascade turns the clear() into DELETEs on flush."""
    vm.disks.clear()
    vm.disks.extend(
        VmDisk(
            position=i,
            disk_id=d.get("id") or None,
            path=d.get("path") or None,
            controller=d.get("controller") or None,
            size_bytes=_as_int(d.get("sizeBytes")),
            used_bytes=_as_int(d.get("usedBytes")),
            type=(d.get("type") or None),
            format=(d.get("format") or None),
        )
        for i, d in enumerate(disks)
    )


def _access_vlan(value) -> int | None:
    """Agent reports 0 (or nothing) for an untagged adapter - store that as NULL."""
    return value if isinstance(value, int) and value > 0 else None


def _sync_nics(vm: Vm, nics: list[dict]) -> None:
    """Replace vm.nics wholesale from the normalized inventory list."""
    vm.nics.clear()
    vm.nics.extend(
        VmNic(
            position=i,
            nic_id=n.get("id") or None,
            name=n.get("name") or "",
            switch_name=n.get("switchName") or None,
            vlan_id=_access_vlan(n.get("vlanId")),
            mac_address=n.get("macAddress") or None,
            ip_addresses=list(n.get("ipAddresses") or []),
            connected=bool(n.get("connected", True)),
        )
        for i, n in enumerate(nics)
    )


def _sync_snapshots(vm: Vm, snapshots: list[dict]) -> None:
    """Replace vm.snapshots wholesale from the normalized inventory list."""
    vm.snapshots.clear()
    vm.snapshots.extend(
        VmSnapshot(
            position=i,
            snapshot_id=s.get("id") or "",
            name=s.get("name") or "",
            parent_snapshot_id=s.get("parentId") or None,
            snapshot_type=s.get("type") or None,
            snapshot_created_at=_parse_dt(s.get("createdAt")),
        )
        for i, s in enumerate(snapshots)
    )


# Column ← (normalized-item key, [raw agent keys that signal "this field was
# reported"]). Used by _write_vm_row so a full inventory and a partial post-action
# status update (agent's lighter vm_status payload) share one field list.
_VM_SCALAR_FIELDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("uptime_sec", "uptimeSec", ("uptimeSec",)),
    ("memory_bytes", "memoryBytes", ("memoryBytes", "memoryMb", "memoryMB", "memory")),
    ("memory_min_bytes", "memoryMinBytes", ("memoryMinBytes", "memory")),
    ("memory_max_bytes", "memoryMaxBytes", ("memoryMaxBytes", "memory")),
    ("memory_demand_bytes", "memoryDemandBytes", ("memoryDemandBytes", "memoryConsumedMb")),
    ("cpu_usage_percent", "cpuUsagePercent", ("cpuUsagePercent", "cpuUsagePct")),
    ("secure_boot", "secureBoot", ("secureBoot",)),
    ("secure_boot_template", "secureBootTemplate", ("secureBootTemplate",)),
    ("auto_start_action", "autoStartAction", ("autoStartAction", "automaticStart")),
    (
        "auto_start_delay_sec",
        "autoStartDelaySec",
        ("autoStartDelaySec", "automaticStartDelay"),
    ),
    ("auto_stop_action", "autoStopAction", ("autoStopAction", "automaticStop")),
    ("config_path", "configPath", ("configPath", "path")),
    ("dvd_path", "dvdPath", ("dvdPath", "dvd")),
    ("notes", "notes", ("notes",)),
)


def _write_vm_row(
    vm: Vm, raw: dict, item: dict, *, host_id: str, cluster_id: str | None, partial: bool
) -> None:
    """Copy a normalized inventory item onto a Vm row. When ``partial`` (a
    post-action status update from the agent's lighter vm_status payload) only
    fields actually present in ``raw`` are touched, so the sparser payload never
    nulls out data a full inventory filled in."""

    def has(*keys: str) -> bool:
        return not partial or any(k in raw for k in keys)

    vm.host_id = host_id  # follows a live-migration within the cluster
    vm.cluster_id = cluster_id
    # folder_id is application-managed (services.organization) - never touched here.

    if has("name") and item.get("name"):
        vm.name = item["name"]
    elif not partial:
        vm.name = item.get("name") or vm.name or vm.vm_uuid

    if has("state", "powerState"):
        vm.state = item.get("state") or ("Unknown" if not partial else vm.state)

    if raw.get("metricsEnabled") is not None:
        vm.metrics_enabled = bool(item.get("metricsEnabled"))

    if has("firmware", "generation") and item.get("firmware"):
        vm.firmware = item["firmware"]
    elif not partial:
        vm.firmware = item.get("firmware") or "UEFI"

    if has("vcpu", "cpu"):
        vm.vcpu = int(item.get("vcpu") or (vm.vcpu if partial else 1) or 1)

    if has("memoryDynamic", "memory"):
        vm.memory_dynamic = bool(item.get("memoryDynamic"))
    if has("nestedVirtualization", "nested"):
        vm.nested_virtualization = bool(item.get("nestedVirtualization"))
    if has("highlyAvailable", "ha"):
        vm.highly_available = bool(item.get("highlyAvailable"))

    for column, item_key, raw_keys in _VM_SCALAR_FIELDS:
        if has(*raw_keys):
            setattr(vm, column, item.get(item_key))

    if has("createdAt", "creationTime") and item.get("createdAt"):
        vm.vm_created_at = _parse_dt(item.get("createdAt"))

    if has("disks", "vhds"):
        _sync_disks(vm, item["disks"])
    if has("nics"):
        _sync_nics(vm, item["nics"])
    if has("snapshots"):
        _sync_snapshots(vm, item["snapshots"])

    vm.last_inventory_at = _now()


async def apply_vm_inventory(db: AsyncSession, host_id: str, payload: dict) -> None:
    host = await db.get(Host, host_id)
    if host is None:
        log.warning("vm_inventory for unknown host=%s", host_id)
        return

    items = payload.get("vms", [])
    scope_ids = await _inventory_scope_host_ids(db, host)
    existing = {
        vm.vm_uuid: vm
        for vm in (
            await db.execute(
                select(Vm)
                .where(Vm.host_id.in_(scope_ids))
                .where(Vm.vm_uuid.is_not(None))
                .options(
                    selectinload(Vm.disks),
                    selectinload(Vm.nics),
                    selectinload(Vm.snapshots),
                )
            )
        ).scalars()
    }
    seen: set[str] = set()
    tombstoned = await list_deleted_vm_uuids(host_id)

    for raw in items:
        item = normalize_vm(raw)
        vm_uuid = item["vmUuid"]
        if not vm_uuid:
            log.warning(
                "vm_inventory host=%s id=%s: item without an id - skipped",
                host.name,
                host.short_id,
            )
            continue

        vm = existing.get(vm_uuid)
        if vm is None and vm_uuid in tombstoned:
            # Just deleted here; this is a stale snapshot from before the delete.
            # Ignore it (and leave it out of `seen`) - a later fresh snapshot
            # that omits the GUID clears the tombstone below.
            log.info(
                "vm_inventory host=%s: ignoring stale entry for deleted vm %s",
                host.name,
                vm_uuid,
            )
            continue
        seen.add(vm_uuid)

        if vm is None:
            vm = Vm(id=new_id(), host_id=host_id, vm_uuid=vm_uuid)
            db.add(vm)
            existing[vm_uuid] = vm

        _write_vm_row(
            vm, raw, item, host_id=host_id, cluster_id=host.cluster_id, partial=False
        )
        await set_vm_state(vm.id, vm.state)

    # drop VMs still owned by THIS host that it no longer reports. Rows now owned
    # by another cluster node (post-migration) are left for that node's inventory.
    for vm in existing.values():
        if vm.host_id == host_id and vm.vm_uuid not in seen:
            await clear_vm_state(vm.id)
            await db.delete(vm)

    # This snapshot confirms the deletion of any tombstoned GUID it no longer
    # lists - drop the tombstone so a future VM reusing that GUID isn't ignored.
    for vm_uuid in tombstoned - seen:
        await clear_vm_deleted(host_id, vm_uuid)

    log.info(
        "vm_inventory host=%s id=%s vms=%d", host.name, host.short_id, len(items)
    )


# ---- host_inventory -----------------------------------------------------

# C-drive path spellings the agent might use, so we don't add a duplicate volume.
_C_DRIVE = {"c:", "c:\\", "c:/"}


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_hardware(raw: dict | None) -> dict | None:
    """Return a canonical `HostHardwareInventory` dict (see schemas/host.py).

    Current agents send the canonical shape already (it round-trips unchanged).
    Rows written by an older agent hold the flat legacy payload (GB floats, separate
    `diskC`, `cpuModel`/`osName`/…); those are mapped here. Returns None for junk.
    """
    if not isinstance(raw, dict) or not raw:
        return None

    # already canonical - the agent's own MarshalJSON output
    if isinstance(raw.get("cpu"), dict):
        return raw

    # --- legacy flat payload → canonical --------------------------------------
    storage: list[dict] = []
    for vol in raw.get("storage") or []:
        if not isinstance(vol, dict):
            continue
        storage.append(
            {
                "path": vol.get("path") or vol.get("name") or "",
                "label": vol.get("name") or None,
                "totalBytes": round(_num(vol.get("totalGb")) * _GIB),
                "freeBytes": round(_num(vol.get("freeGb")) * _GIB),
            }
        )

    disk_c = raw.get("diskC")
    if isinstance(disk_c, dict) and not any(
        v["path"].lower() in _C_DRIVE for v in storage
    ):
        storage.insert(
            0,
            {
                "path": "C:\\",
                "totalBytes": round(_num(disk_c.get("totalGb")) * _GIB),
                "freeBytes": round(_num(disk_c.get("freeGb")) * _GIB),
            },
        )

    cpu_model = raw.get("cpuModel") or ""
    os_name = raw.get("osName") or ""
    memory_bytes = round(_num(raw.get("memoryCapacityMB")) * _MIB)
    if not (cpu_model or os_name or memory_bytes or storage):
        return None  # nothing recognizable - not a hardware payload

    logical = int(_num(raw.get("logicalProcessors")))
    out: dict = {
        "cpu": {
            "model": cpu_model,
            "sockets": int(_num(raw.get("cpuSockets"))) or (1 if logical else 0),
            "cores": int(_num(raw.get("cpuCores"))) or logical,
            "logical": logical,
        },
        "memoryBytes": memory_bytes,
        "storage": storage,
        "os": {"caption": os_name, "version": raw.get("osVersion") or ""},
        "network": [],
        "vSwitches": [],
        "load": {
            "cpuPercent": _num(raw.get("cpuLoadPercent")),
            "memoryPercent": _num(raw.get("memoryUsagePercent")),
        },
    }
    if raw.get("hwManufacturer") or raw.get("hwModel"):
        out["system"] = {
            "manufacturer": raw.get("hwManufacturer") or "",
            "model": raw.get("hwModel") or "",
        }
    if raw.get("lastBootTime"):
        out["bootTime"] = raw["lastBootTime"]
    if raw.get("clusterState") or raw.get("isCluster"):
        out["cluster"] = {
            "clustered": bool(raw.get("isCluster")),
            "name": raw.get("clusterName") or None,
            "state": raw.get("clusterState") or "",
            "nodes": raw.get("clusterNodes") or [],
        }
    if raw.get("defaultVmPath") or raw.get("defaultVhdPath"):
        out["hyperv"] = {
            "defaultVmPath": raw.get("defaultVmPath") or None,
            "defaultVhdPath": raw.get("defaultVhdPath") or None,
        }
    return out


async def apply_host_inventory(db: AsyncSession, host_id: str, payload: dict) -> None:
    host = await db.get(Host, host_id)
    if host is None:
        log.warning("host_inventory for unknown host=%s", host_id)
        return
    # Only hardware - folders are application-managed, not agent-reported.
    # Accept both {reported_at, hardware:{...}} and a bare hardware dict.
    raw = payload["hardware"] if "hardware" in payload else payload
    normalized = normalize_hardware(raw)
    if normalized is None:
        # empty / malformed report - keep the last known-good hardware
        log.warning(
            "host_inventory host=%s id=%s: no usable hardware (kept previous)",
            host.name,
            host.short_id,
        )
        return
    host.hardware = normalized
    log.info("host_inventory host=%s id=%s", host.name, host.short_id)


# ---- host_metrics / vm_metrics (quick metrics, last hour only) ---------

def _metrics_cutoff() -> datetime:
    return _now() - timedelta(seconds=get_settings().metrics_retention_seconds)


def _as_int(value) -> int | None:
    try:
        return int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def apply_host_metrics(db: AsyncSession, host_id: str, payload: dict) -> None:
    host = await db.get(Host, host_id)
    if host is None:
        log.warning("host_metrics for unknown host=%s", host_id)
        return
    db.add(
        HostMetric(
            host_id=host_id,
            ts=_parse_dt(payload.get("reportedAt") or payload.get("reported_at")) or _now(),
            cpu_percent=_as_float(payload.get("cpuPercent")),
            mem_percent=_as_float(payload.get("memPercent")),
            disk_latency_ms=_as_float(payload.get("diskLatencyMs")),
            net_rx_bps=_as_int(payload.get("netRxBps")),
            net_tx_bps=_as_int(payload.get("netTxBps")),
            detail=payload,
        )
    )
    await db.execute(
        delete(HostMetric)
        .where(HostMetric.host_id == host_id)
        .where(HostMetric.ts < _metrics_cutoff())
    )
    log.info("host_metrics host=%s id=%s", host.name, host.short_id)


async def apply_vm_metrics(db: AsyncSession, host_id: str, payload: dict) -> None:
    items = payload.get("vms", [])
    hlabel = await _host_label(db, host_id)
    ts = _parse_dt(payload.get("reportedAt") or payload.get("reported_at")) or _now()
    # agent keys samples by the Hyper-V GUID; translate to our internal id. Only
    # metered VMs get a vm_metrics row - the time-series is opt-in per VM.
    by_uuid = {
        row.vm_uuid: row.id
        for row in (
            await db.execute(
                select(Vm.id, Vm.vm_uuid).where(
                    Vm.host_id == host_id, Vm.metrics_enabled.is_(True)
                )
            )
        ).all()
        if row.vm_uuid
    }
    inserted = 0
    skipped = 0
    for item in items:
        vm_uuid = item.get("id") or item.get("vmId")
        vm_id = by_uuid.get(vm_uuid)
        if vm_id is None:
            # unknown VM, or one without metering enabled - nothing to store
            skipped += 1
            continue
        db.add(
            VmMetric(
                vm_id=vm_id,
                ts=ts,
                cpu_percent=_as_float(item.get("cpuPercent")),
                mem_bytes=_as_int(item.get("memBytes")),
                disk_bytes=_as_int(item.get("diskBytes")),
                net_rx_bytes=_as_int(item.get("netRxBytes")),
                net_tx_bytes=_as_int(item.get("netTxBytes")),
                detail=item,
            )
        )
        inserted += 1

    await db.execute(
        delete(VmMetric)
        .where(VmMetric.ts < _metrics_cutoff())
        .where(VmMetric.vm_id.in_(select(Vm.id).where(Vm.host_id == host_id)))
    )
    log.info("vm_metrics %s vms=%d skipped=%d", hlabel, inserted, skipped)


# ---- template_inventory / iso_inventory --------------------------------

def _template_row_fields(host_id: str, item: dict) -> dict:
    """Map one `template_inventory` item (or a `vm_export_template` response's
    `template`) to Template column kwargs - used by both entry points."""
    return dict(
        host_id=host_id,
        name=item.get("name", ""),
        path=item.get("path", ""),
        size_bytes=int(item.get("sizeBytes", 0)),
        disk_size_bytes=int(item.get("diskSizeBytes", 0)),
        notes=item.get("notes") or None,
        cpu_count=int(item.get("cpuCount") or 0),
        memory_mb=int(item.get("memoryMb") or 0),
        guest_os=item.get("guestOs"),
        image_created_at=_parse_dt(item.get("createdAt")),
    )


async def _upsert_template(db: AsyncSession, host_id: str, item: dict) -> None:
    """Insert-or-update a single Template row by its id (the stable UUID from
    `ovc-template-metadata.json`). The next full `template_inventory` reconciles
    it either way - this just avoids the wait."""
    tid = item.get("id") or new_id()
    fields = _template_row_fields(host_id, item)
    row = await db.get(Template, tid)
    if row is None:
        db.add(Template(id=tid, **fields))
    else:
        for k, v in fields.items():
            setattr(row, k, v)


async def apply_template_inventory(
    db: AsyncSession, host_id: str, payload: dict
) -> None:
    await db.execute(delete(Template).where(Template.host_id == host_id))
    for item in payload.get("items", []):
        db.add(Template(id=item.get("id") or new_id(), **_template_row_fields(host_id, item)))
    log.info(
        "template_inventory %s items=%d",
        await _host_label(db, host_id),
        len(payload.get("items", [])),
    )


async def apply_iso_inventory(db: AsyncSession, host_id: str, payload: dict) -> None:
    await db.execute(delete(Iso).where(Iso.host_id == host_id))
    for item in payload.get("items", []):
        db.add(
            Iso(
                # Always a fresh random id. The agent sends the ISO's content
                # MD5 as `id`, which collides when the same ISO exists on more
                # than one host (isos.id is a global PK, and the DELETE above
                # only clears this host's rows). Nothing keys off the id - the
                # table is rebuilt per host on every inventory and DVD mounts
                # use `path` - so a UUID is safe. The MD5 is kept as `checksum`
                # so the UI can flag same-name / different-build ISOs.
                id=new_id(),
                host_id=host_id,
                name=item.get("name", ""),
                path=item.get("path", ""),
                size_bytes=int(item.get("sizeBytes", 0)),
                checksum=item.get("checksum") or None,
            )
        )
    log.info(
        "iso_inventory %s items=%d",
        await _host_label(db, host_id),
        len(payload.get("items", [])),
    )


# ---- response (task lifecycle) ---------------------------------------

# Optimistic state _queue_vm_task stamps for each power action, and the state the
# VM was almost certainly still in if that action *failed*. Used to roll the row
# back so a failed start/stop doesn't leave the VM stuck "Starting" in the UI
# until the agent's next periodic vm_inventory.
_TRANSITIONAL_STATE = {
    "vm_start": "Starting",
    "vm_stop": "Stopping",
    "vm_shutdown": "Stopping",
    "vm_restart": "Restarting",
    "vm_pause": "Pausing",
}
_FAILED_STATE_REVERT = {
    "vm_start": "Off",
    "vm_stop": "Running",
    "vm_shutdown": "Running",
    "vm_restart": "Running",
    "vm_pause": "Running",
}


async def apply_response(db: AsyncSession, host_id: str, resp: AgentResponse) -> None:
    task = (
        await db.execute(
            select(Task).where(Task.id == resp.id).where(Task.host_id == host_id)
        )
    ).scalar_one_or_none()
    if task is None:
        # The agent runs its own periodic inventory/status/metrics refreshes and
        # publishes a response envelope for each (id "refresh-<kind>-<ts>"). There
        # is no backend task and the real payload arrives on the dedicated queue,
        # so drop these quietly; warn only for a genuinely orphaned response.
        if resp.id.startswith("refresh-"):
            log.debug("periodic refresh response id=%s host=%s (ignored)", resp.id, host_id)
        else:
            log.warning("response for unknown task id=%s host=%s", resp.id, host_id)
        return

    # Keep the raw envelope for the task "Details" view - the latest one wins, so
    # a finished task ends up with its terminal response.
    task.response_payload = resp.model_dump(mode="json")

    if resp.status == "running" and task.status in ("queued", "running"):
        task.status = "running"
        task.started_at = task.started_at or _now()
        if resp.progress is not None:
            task.progress = resp.progress
        if resp.progress_message:
            task.progress_message = resp.progress_message
        return

    # terminal
    task.status = "succeeded" if resp.status == "succeeded" else "failed"
    task.finished_at = _parse_dt(resp.finished_at) or _now()
    task.progress = 100 if task.status == "succeeded" else task.progress
    task.progress_message = None
    task.result = resp.result if isinstance(resp.result, str) else None
    task.error = resp.error
    # A vm_delete that succeeded but left files behind (remove_files) carries a
    # `warning` in its result - keep it on the task record for the UI/audit.
    if (
        task.status == "succeeded"
        and not task.error
        and isinstance(resp.result, dict)
        and resp.result.get("warning")
    ):
        task.error = str(resp.result["warning"])
        log.warning("task %s succeeded with warning: %s", task.id, task.error)
    # Persist the lifecycle change before the best-effort row refresh below, so a
    # malformed agent payload can't roll it back with the rest of the session.
    await db.flush()

    # An agent upgrade's terminal "succeeded" is published by the NEW agent after
    # it restarts; stamp the version now so the UI doesn't wait for the next
    # agent_status heartbeat (~60s).
    if task.kind == "host_update_agent" and task.status == "succeeded":
        version = resp.result.get("version") if isinstance(resp.result, dict) else None
        if version:
            host = await db.get(Host, host_id)
            if host is not None:
                host.agent_version = str(version)
                await db.flush()

    # cluster node pause/resume: reflect the node state the agent reported now
    # rather than at the next hardware inventory. Best-effort.
    if task.target_type == "host":
        # local import: host_actions -> serializers -> inventory is a cycle
        from .host_actions import apply_host_action_response

        try:
            async with db.begin_nested():
                await apply_host_action_response(db, task, resp)
        except Exception:
            log.exception("apply_response: could not apply host state for task %s", task.id)

    # vm_export_template: register the new template row now (the agent attaches
    # the fresh template object to `resp.template`) rather than waiting for the
    # next template_inventory. Best-effort, in its own SAVEPOINT.
    if (
        task.status == "succeeded"
        and task.kind == "vm_export_template"
        and isinstance(resp.template, dict)
    ):
        try:
            async with db.begin_nested():
                await _upsert_template(db, task.host_id, resp.template)
                await db.flush()
            log.info("task %s registered template %s", task.id, resp.template.get("name"))
        except Exception:
            log.exception("apply_response: could not register template for task %s", task.id)

    # keep the VM row + cache fresh immediately (don't wait for the next
    # vm_inventory - in the worker-process model the agent can't publish one).
    # The agent attaches the fresh full VM object to `resp.vm_status` (or the
    # older `resp.result` = {"vms": [...]}) after a VM-mutating task.
    #
    # This is best-effort: it runs in a SAVEPOINT so a bad payload rolls back
    # only this block, never the terminal status above or the lock release
    # below. The next periodic vm_inventory reconciles the row regardless.
    try:
        async with db.begin_nested():
            await _apply_response_state(db, host_id, resp, task)
    except Exception:
        log.exception("apply_response: could not apply VM state for task %s", task.id)

    # the task is done - free the VM for the next operation. Never let a cache
    # hiccup here strand the lock's owner: the TTL is the ultimate backstop.
    if task.target_type == "vm":
        try:
            await release_vm_lock(task.target_id, task.id)
        except Exception:
            log.exception("apply_response: could not release lock for vm %s", task.target_id)

    log.info("task %s -> %s", task.id, task.status)


async def _apply_response_state(
    db: AsyncSession, host_id: str, resp: AgentResponse, task: Task
) -> None:
    """Fold the agent's post-task VM snapshot into the row + state cache. Called
    inside a SAVEPOINT by `apply_response` - may raise on a malformed payload."""
    if task.kind in ("vm_create", "vm_clone"):
        # placeholder row created by request_vm_create / request_vm_clone - stamp
        # the real GUID + state the agent just assigned, or drop the row if the
        # create/clone failed.
        placeholder = await db.get(Vm, task.target_id)
        if task.status == "succeeded" and placeholder is not None:
            if resp.vm_id:
                placeholder.vm_uuid = resp.vm_id
            placeholder.state = resp.vm_state or "Off"
            await set_vm_state(placeholder.id, placeholder.state)
            await db.flush()
            await _apply_response_vm_status(db, host_id, resp)
        elif placeholder is not None:
            await db.execute(delete(Vm).where(Vm.id == placeholder.id))
            await clear_vm_state(placeholder.id)
    elif task.kind == "vm_delete" and task.status == "succeeded":
        # Tombstone the Hyper-V GUID so a periodic vm_inventory the agent
        # captured before the deletion can't resurrect the row when the worker
        # consumes it (see cache.mark_vm_deleted).
        gone = await db.get(Vm, task.target_id)
        if gone is not None and gone.vm_uuid:
            await mark_vm_deleted(host_id, gone.vm_uuid)
        await db.execute(delete(Vm).where(Vm.id == task.target_id))
        await clear_vm_state(task.target_id)
    elif task.status == "succeeded":
        applied = await _apply_response_vm_status(db, host_id, resp)
        # fall back to the bare vm_id/vm_state hint if no VM object came
        if resp.vm_id and resp.vm_state and not applied:
            vm = (
                await db.execute(
                    select(Vm)
                    .where(Vm.vm_uuid == resp.vm_id)
                    .where(Vm.host_id == host_id)
                )
            ).scalar_one_or_none()
            if vm is not None:
                vm.state = resp.vm_state
                await set_vm_state(vm.id, resp.vm_state)
    elif task.status == "failed":
        # A failed power action leaves the optimistic transitional state
        # (_queue_vm_task set e.g. "Starting") stuck on the row + cache until the
        # agent's next periodic vm_inventory. Roll it back to the state the VM was
        # almost certainly still in; the periodic inventory confirms it later.
        revert = _FAILED_STATE_REVERT.get(task.kind)
        if revert and task.target_type == "vm":
            vm = await db.get(Vm, task.target_id)
            if vm is not None and vm.state == _TRANSITIONAL_STATE.get(task.kind):
                vm.state = revert
                await set_vm_state(vm.id, revert)


def _response_vm_items(resp: AgentResponse) -> list[dict]:
    """The fresh VM object(s) an agent attached to a response, from `vm_status`
    (a bare VMInfo, or a {"vms": [...]} wrapper) or the legacy `result` wrapper."""
    for src in (resp.vm_status, resp.result):
        if not isinstance(src, dict):
            continue
        if isinstance(src.get("vms"), list):
            return [v for v in src["vms"] if isinstance(v, dict)]
        if src.get("id") or src.get("vmId"):
            return [src]
    return []


async def _apply_response_vm_status(
    db: AsyncSession, host_id: str, resp: AgentResponse
) -> bool:
    """Apply the VM object(s) an agent attached to a task response. Partial -
    only fields the payload carries are written (see `_write_vm_row`). Never
    creates or deletes rows; an unknown VM waits for the next full inventory.
    Returns True if at least one row was updated."""
    items = _response_vm_items(resp)
    if not items:
        return False
    host = await db.get(Host, host_id)
    if host is None:
        return False
    updated = False
    for raw in items:
        item = normalize_vm(raw)
        vm_uuid = item["vmUuid"]
        if not vm_uuid:
            continue
        vm = (
            await db.execute(
                select(Vm)
                .where(Vm.vm_uuid == vm_uuid)
                .where(Vm.host_id == host_id)
                .options(
                    selectinload(Vm.disks),
                    selectinload(Vm.nics),
                    selectinload(Vm.snapshots),
                )
            )
        ).scalar_one_or_none()
        if vm is None:
            continue
        _write_vm_row(
            vm, raw, item, host_id=host_id, cluster_id=host.cluster_id, partial=True
        )
        await set_vm_state(vm.id, vm.state)
        updated = True
    return updated

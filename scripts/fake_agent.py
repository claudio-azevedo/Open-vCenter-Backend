"""A stand-in for ovc-agent so the whole stack is demoable without Windows hosts.

For every host in the DB it:
  - consumes <hostid>.request, simulates the operation, replies on <hostid>.response
  - periodically posts agent_status / host_inventory / vm_inventory / *_inventory

The randomly generated hardware/VM/template/ISO inventory is written to a local
JSON file on first run (OVC_FAKE_AGENT_STATE_FILE, default scripts/.fake_agent_state.json)
and reloaded on restart, so the agent keeps reporting the same hosts and VMs.
Simulated actions (start/stop/delete/...) mutate that file too.

    python scripts/fake_agent.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aio_pika
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging import configure_logging
from app.messaging import QueueKind, declare_host_queues, queue_name
from app.models import Host

log = logging.getLogger("fake-agent")
_settings = get_settings()
GiB = 1024**3


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hardware() -> dict:
    return {
        "cpu": {"model": "Intel Xeon Gold 6338", "sockets": 2, "cores": 32, "logical": 64},
        "memoryBytes": 512 * GiB,
        "storage": [
            {"path": "C:\\", "totalBytes": 480 * GiB, "freeBytes": 120 * GiB},
            {"path": "D:\\HyperV", "totalBytes": 8192 * GiB, "freeBytes": 3300 * GiB},
            {"path": "E:\\", "totalBytes": 4096 * GiB, "freeBytes": 4000 * GiB},
        ],
        "hyperv": {
            "defaultVmPath": "D:\\HyperV\\VMS",
            "defaultVhdPath": "D:\\HyperV\\VHDS",
        },
        "os": {"caption": "Windows Server 2022 Datacenter", "version": "10.0.20348"},
        "network": [
            {
                "name": "Team-Prod",
                "description": "Intel(R) Ethernet Converged Network Adapter X710 #1",
                "mac": "00:15:5D:01:0A:01",
                "speedBps": 25_000_000_000,
                "connected": True,
            },
            {
                "name": "Team-Prod #2",
                "description": "Intel(R) Ethernet Converged Network Adapter X710 #2",
                "mac": "00:15:5D:01:0A:02",
                "speedBps": 25_000_000_000,
                "connected": True,
            },
            {
                "name": "Mgmt",
                "description": "Broadcom NetXtreme Gigabit Ethernet",
                "mac": "00:15:5D:01:0A:03",
                "speedBps": 1_000_000_000,
                "connected": False,
            },
        ],
        "vSwitches": [
            {"name": "vSwitch-Prod", "type": "External", "netAdapter": "Team-Prod"},
            {"name": "vSwitch-Mgmt", "type": "Internal", "netAdapter": None},
        ],
    }


def _gen_vms(host_id: str, name: str) -> list[dict]:
    base = name.split("-")[-1]
    out = []
    for i in range(1, random.randint(3, 6)):
        state = random.choice(["Running", "Running", "Off", "Paused"])
        vm_id = f"{host_id}-{i:04d}-vm00-0000-000000000000"
        out.append(
            {
                "id": vm_id,
                "name": f"{name}-VM{i:02d}",
                "state": state,
                "firmware": random.choice(["UEFI", "UEFI", "BIOS"]),
                "folderId": None,
                "uptimeSec": random.randint(0, 400000) if state == "Running" else None,
                "vcpu": random.choice([2, 4, 8]),
                "memoryMb": random.choice([4, 8, 16]) * 1024,
                "memoryMinBytes": 2 * GiB,
                "memoryMaxBytes": 16 * GiB,
                "memoryDynamic": True,
                "memoryConsumedMb": random.randint(1, 8) * 1024
                if state == "Running"
                else 0,
                "cpuUsagePct": random.randint(0, 40) if state == "Running" else 0,
                "secureBoot": random.choice([True, False]),
                "secureBootTemplate": random.choice(["Windows", "Linux", "Others"]),
                "nestedVirtualization": random.choice([True, False, False]),
                "automaticStart": random.choice(
                    ["Nothing", "StartIfRunning", "Start"]
                ),
                "automaticStartDelay": random.choice([0, 0, 30, 120]),
                "automaticStop": random.choice(["Save", "ShutDown", "TurnOff"]),
                "path": f"D:\\HyperV\\VMs\\{base}{i}",
                "dvd": random.choice(
                    [None, None, "D:\\HyperV\\ISOS\\Rocky-9.7-x86_64-minimal.iso"]
                ),
                "ha": random.choice([True, False]),
                "disks": [
                    {
                        "id": f"{vm_id}-os",
                        "path": f"D:\\HyperV\\VMs\\{base}{i}\\{base}{i}.vhdx",
                        "controller": "SCSI 0:0",
                        "sizeBytes": 127 * GiB,
                        "usedBytes": 40 * GiB,
                        "type": "Dynamic",
                        "format": "VHDX",
                    }
                ],
                "nics": [
                    {
                        "id": f"{vm_id}-nic0",
                        "name": "Network Adapter",
                        "switchName": "vSwitch-Prod",
                        "vlanId": random.choice([0, 0, 100, 200]),
                        "macAddress": f"00:15:5D:01:0A:{i:02d}",
                        "ipAddresses": [f"10.20.0.{10 + i}"] if state == "Running" else [],
                        "connected": True,
                    }
                ],
                "snapshots": (
                    [
                        {
                            "id": f"{vm_id}-snap0",
                            "name": "before-update",
                            "parentId": None,
                            "type": "Standard",
                            "createdAt": _now(),
                        }
                    ]
                    if random.random() < 0.4
                    else []
                ),
                "notes": None,
                "createdAt": _now(),
            }
        )
    return out


# --- local persistence -------------------------------------------------------
# Keeps generated inventory stable across restarts.

STATE_FILE = Path(
    os.environ.get("OVC_FAKE_AGENT_STATE_FILE")
    or Path(__file__).resolve().parent / ".fake_agent_state.json"
)

_state: dict = {"hosts": {}}
_state_lock = asyncio.Lock()

# Canned progress streams per function - (percent, progress_message) - so the
# real backend stores a live "Details" line and the UI can render it.
_FAKE_PROGRESS_STEPS: dict[str, list[tuple[int, str]]] = {
    "vm_create": [
        (10, "Creating VM"),
        (40, "Creating disk 1/1: OS.vhdx"),
        (75, "Configuring VM settings"),
        (95, "Starting VM"),
    ],
    "vm_clone": [
        (10, "Exporting source VM (0%)"),
        (45, "Importing VM and copying disks"),
        (82, "Expanding disks to fixed"),
        (85, "Configuring VM settings"),
    ],
    "vm_export_template": [
        (20, "Exporting VM (35%)"),
        (60, "Exporting VM (80%)"),
        (87, "Converting fixed disks to dynamic"),
        (90, "Reading template details"),
    ],
    "vm_edit": [(30, "Applying changes"), (70, "Expanding disk 1/1: DATA.vhdx")],
    "vm_move": [(20, "Moving VM storage (this may take a while)")],
}


def _load_state() -> None:
    global _state
    if STATE_FILE.exists():
        try:
            _state = json.loads(STATE_FILE.read_text())
            _state.setdefault("hosts", {})
            log.info("loaded fake-agent state from %s (%d hosts)", STATE_FILE, len(_state["hosts"]))
            return
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read %s (%s) - regenerating", STATE_FILE, exc)
    _state = {"hosts": {}}


async def _save_state() -> None:
    async with _state_lock:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state, indent=2))
        tmp.replace(STATE_FILE)


def _host_state(host_id: str, name: str) -> dict:
    """Return the persisted inventory for a host, generating it once on first sight."""
    hs = _state["hosts"].get(host_id)
    if hs is None:
        hs = _state["hosts"][host_id] = {
            "name": name,
            "hardware": _hardware(),
            "vms": _gen_vms(host_id, name),
            "templates": [{
                "id": f"{host_id}-tpl-1", "name": "W2022-Base",
                "path": "D:\\HyperV\\TEMPLATES\\w2022.vhdx",
                "sizeBytes": 18 * GiB, "diskSizeBytes": 6 * GiB,
                "notes": "Sysprepped Windows Server 2022 base image.",
                "cpuCount": 4, "memoryMb": 8192,
                "guestOs": "Windows Server 2022",
                "createdAt": _now(),
            }],
            "isos": [{
                "id": f"{host_id}-iso-1", "name": "WinServer2022.iso",
                "path": "D:\\HyperV\\ISOS\\ws2022.iso", "sizeBytes": 5 * GiB,
            }],
        }
        log.info("[%s] generated %d VM(s)", name, len(hs["vms"]))
    return hs


_CSV_RE = re.compile(r"^([a-zA-Z]:\\ClusterStorage\\[^\\]+)", re.IGNORECASE)


def _resolve_vm_folder(default_vm_path: str, destination_storage: str, name: str) -> str:
    """Mirror of the agent's resolveVMFolder - the per-VM folder for a create."""
    default = default_vm_path.rstrip("\\/")
    dest = destination_storage.rstrip("\\/")
    if not dest:
        return f"{default}\\{name}"
    csv = _CSV_RE.match(dest)
    if csv:
        return f"{csv.group(1)}\\VMS\\{name}"
    dv = default[:2].lower() if len(default) > 1 and default[1] == ":" else ""
    if dv and dest[:2].lower() == dv:
        return f"{default}\\{name}"
    vol = dest[:2] if len(dest) > 1 and dest[1] == ":" else dest
    return f"{vol}\\HyperV\\VMS\\{name}"


def _apply_action(host_id: str, vm_id: str | None, fn: str, final_state: str | None) -> None:
    """Mutate persisted VM state to reflect a simulated action."""
    hs = _state["hosts"].get(host_id)
    if hs is None or vm_id is None:
        return
    if fn == "vm_delete":
        hs["vms"] = [v for v in hs["vms"] if v["id"] != vm_id]
        return
    if fn in ("vm_enable_metrics", "vm_disable_metrics"):
        for vm in hs["vms"]:
            if vm["id"] == vm_id:
                vm["metricsEnabled"] = fn == "vm_enable_metrics"
        return
    if final_state is None:
        return
    for vm in hs["vms"]:
        if vm["id"] != vm_id:
            continue
        vm["state"] = final_state
        if final_state == "Running":
            vm["uptimeSec"] = vm.get("uptimeSec") or 0
        else:
            vm["uptimeSec"] = None
            for nic in vm.get("nics", []):
                nic["ipAddresses"] = []


def _apply_vm_edit(host_id: str, vm_id: str | None, params: dict) -> dict | None:
    """Simulate `vm_edit`: mutate the persisted VM and echo it back as vm_status."""
    hs = _state["hosts"].get(host_id)
    if hs is None or vm_id is None:
        return None
    vm = next((v for v in hs["vms"] if v["id"] == vm_id), None)
    if vm is None:
        return None

    if params.get("cpu_count"):
        vm["vcpu"] = int(params["cpu_count"])
    if params.get("memory_mb"):
        vm["memoryMb"] = int(params["memory_mb"])
        vm["memoryBytes"] = int(params["memory_mb"]) * 1024 * 1024
    for key in ("notes",):
        if params.get(key) is not None:
            vm[key] = params[key]
    if params.get("nested_virtualization") is not None:
        vm["nestedVirtualization"] = bool(params["nested_virtualization"])
    if params.get("secure_boot") is not None:
        vm["secureBoot"] = bool(params["secure_boot"])
    if params.get("secure_boot_template"):
        vm["secureBootTemplate"] = params["secure_boot_template"]
    if params.get("automatic_start"):
        vm["autoStartAction"] = params["automatic_start"]
    if params.get("automatic_start_delay") is not None:
        vm["autoStartDelaySec"] = int(params["automatic_start_delay"])
    if params.get("automatic_stop"):
        vm["autoStopAction"] = params["automatic_stop"]

    nics = vm.setdefault("nics", [])
    default_switch = (hs["hardware"].get("vSwitches") or [{}])[0].get("name") or "vSwitch"
    for spec in params.get("remove_nic") or []:
        nics[:] = [n for n in nics if n["id"] != spec.get("nic_id")]
    for spec in params.get("edit_nic") or []:
        n = next((n for n in nics if n["id"] == spec.get("nic_id")), None)
        if n is None:
            continue
        if spec.get("name"):
            n["name"] = spec["name"]
        if spec.get("vlan_id") is not None:
            n["vlanId"] = int(spec["vlan_id"]) or None
        if spec.get("switch_name"):
            n["switchName"] = spec["switch_name"]
    for i, spec in enumerate(params.get("add_nic") or []):
        nics.append({
            "id": f"{vm_id}-nic{len(nics) + i + 1}",
            "name": spec.get("name") or f"Network Adapter {len(nics) + 1}",
            "switchName": spec.get("switch_name") or default_switch,
            "vlanId": int(spec["vlan_id"]) or None if spec.get("vlan_id") else None,
            "macAddress": "00:15:5D:0A:0B:%02X" % ((len(nics) + i) & 0xFF),
            "ipAddresses": [],
            "connected": True,
        })

    disks = vm.setdefault("disks", [])
    for spec in params.get("remove_disk") or []:
        disks[:] = [d for d in disks if d["path"] != spec.get("path")]
    for spec in params.get("edit_disk") or []:
        d = next((d for d in disks if d["path"] == spec.get("path")), None)
        if d and spec.get("size_gb"):
            d["sizeBytes"] = int(float(spec["size_gb"]) * GiB)
    for i, spec in enumerate(params.get("add_disk") or []):
        disks.append({
            "id": f"{vm_id}-disk{len(disks) + i}",
            "path": spec.get("path") or f"D:\\HyperV\\{vm['name']}-extra{i}.vhdx",
            "controller": f"SCSI 0:{len(disks) + i}",
            "sizeBytes": int(float(spec.get("size_gb") or 60) * GiB),
            "usedBytes": 0,
            "type": spec.get("type") or "Dynamic",
            "format": "VHDX",
        })

    return vm


async def _publish(channel, host_id: str, kind: QueueKind, payload: dict) -> None:
    await channel.default_exchange.publish(
        aio_pika.Message(body=json.dumps(payload).encode(), content_type="application/json"),
        routing_key=queue_name(host_id, kind),
    )


async def _serve_host(connection, host_id: str, name: str) -> None:
    channel = await connection.channel()
    queues = await declare_host_queues(channel, host_id)
    hs = _host_state(host_id, name)

    async def handle_request(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            req = json.loads(message.body.decode())
            fn, task_id = req["function"], req["id"]
            params = req.get("params", {})
            vm_id = params.get("vm_id")
            log.info("[%s] %s (%s)", name, fn, task_id)
            # Stream a few progress steps with human-readable text so the UI's
            # "Details" column has something to show.
            steps = _FAKE_PROGRESS_STEPS.get(fn, [(30, "Working…")])
            for pct, msg in steps:
                await _publish(channel, host_id, QueueKind.RESPONSE, {
                    "id": task_id, "function": fn, "status": "running",
                    "progress": pct, "progress_message": msg,
                })
                await asyncio.sleep(random.uniform(0.6, 1.4))

            if fn == "vm_create":
                new_guid = str(uuid.uuid4())
                created_state = "Running" if params.get("start_now") else "Off"
                mem_mb = int(params.get("memory_mb") or 4096)
                mem_dynamic = bool(params.get("memory_dynamic"))
                mem_min_mb = params.get("memory_min_mb") or (mem_mb if mem_dynamic else None)
                mem_max_mb = params.get("memory_max_mb") or (mem_mb * 4 if mem_dynamic else None)
                vm_name = params.get("name") or f"vm-{new_guid[:8]}"
                vlan = params.get("vlan_id")
                default_vm_path = (
                    (hs["hardware"].get("hyperv") or {}).get("defaultVmPath")
                    or "D:\\HyperV\\VMS"
                )
                base_path = _resolve_vm_folder(
                    default_vm_path, params.get("destination_storage") or "", vm_name
                )
                disks = []
                for di, d in enumerate(params.get("disks") or [{"name": "OS"}]):
                    dname = d.get("name", "OS")
                    disks.append({
                        "id": f"{new_guid}-{dname}",
                        "path": f"{base_path}\\Virtual Hard Disks\\{vm_name}-{dname}.vhdx",
                        "controller": f"SCSI 0:{di}",
                        "sizeBytes": int(d.get("size_gb") or 60) * GiB,
                        "usedBytes": 0,
                        "type": d.get("type") or "Fixed",
                        "format": "VHDX",
                    })
                hs["vms"].append({
                    "id": new_guid,
                    "name": vm_name,
                    "state": created_state,
                    "firmware": params.get("firmware") or "UEFI",
                    "folderId": None,
                    "uptimeSec": 0 if created_state == "Running" else None,
                    "vcpu": int(params.get("cpu_count") or 2),
                    "memoryMb": mem_mb,
                    "memoryMinBytes": mem_min_mb * 1024 * 1024 if mem_min_mb else None,
                    "memoryMaxBytes": mem_max_mb * 1024 * 1024 if mem_max_mb else None,
                    "memoryDynamic": mem_dynamic,
                    "nestedVirtualization": bool(
                        params.get("nested_virtualization")
                    ),
                    "ha": bool(params.get("ha_enabled")),
                    "path": base_path,
                    "dvd": params.get("dvd"),
                    "disks": disks,
                    "nics": [{
                        "id": f"{new_guid}-nic0",
                        "name": "Network Adapter",
                        "switchName": params.get("switch_name") or "vSwitch-Prod",
                        "vlanId": int(vlan) if isinstance(vlan, int) else 0,
                        "macAddress": "00:15:5D:0A:0B:0C",
                        "ipAddresses": [],
                        "connected": True,
                    }],
                    "snapshots": [],
                    "notes": params.get("notes"), "metricsEnabled": False,
                    "createdAt": _now(),
                })
                await _save_state()
                await _publish(channel, host_id, QueueKind.RESPONSE, {
                    "id": task_id, "function": fn, "status": "succeeded", "progress": 100,
                    "result": "created", "vm_id": new_guid, "vm_state": created_state,
                    "finished_at": _now(),
                })
                return

            if fn == "vm_clone":
                new_guid = str(uuid.uuid4())
                created_state = "Running" if params.get("start_now") else "Off"
                mem_mb = int(params.get("memory_mb") or 4096)
                vm_name = params.get("name") or f"vm-{new_guid[:8]}"
                vlan = params.get("vlan_id")
                default_vm_path = (
                    (hs["hardware"].get("hyperv") or {}).get("defaultVmPath")
                    or "D:\\HyperV\\VMS"
                )
                base_path = _resolve_vm_folder(
                    default_vm_path, params.get("destination_storage") or "", vm_name
                )
                hs["vms"].append({
                    "id": new_guid, "name": vm_name, "state": created_state,
                    "firmware": "UEFI", "folderId": None,
                    "uptimeSec": 0 if created_state == "Running" else None,
                    "vcpu": int(params.get("cpu_count") or 2), "memoryMb": mem_mb,
                    "memoryDynamic": False,
                    "nestedVirtualization": bool(params.get("nested_virtualization")),
                    "ha": bool(params.get("ha_enabled")), "path": base_path, "dvd": None,
                    "disks": [{
                        "id": f"{new_guid}-OS",
                        "path": f"{base_path}\\Virtual Hard Disks\\{vm_name}-OS.vhdx",
                        "controller": "SCSI 0:0", "sizeBytes": 60 * GiB, "usedBytes": 0,
                        "type": "Dynamic", "format": "VHDX",
                    }],
                    "nics": [{
                        "id": f"{new_guid}-nic0", "name": "Network Adapter",
                        "switchName": "vSwitch-Prod",
                        "vlanId": int(vlan) if isinstance(vlan, int) else 0,
                        "macAddress": "00:15:5D:0A:0B:0D", "ipAddresses": [], "connected": True,
                    }],
                    "snapshots": [], "notes": params.get("notes"), "metricsEnabled": False,
                    "createdAt": _now(),
                })
                await _save_state()
                await _publish(channel, host_id, QueueKind.RESPONSE, {
                    "id": task_id, "function": fn, "status": "succeeded", "progress": 100,
                    "result": "cloned", "vm_id": new_guid, "vm_state": created_state,
                    "finished_at": _now(),
                })
                return

            if fn == "vm_edit":
                vm_status = _apply_vm_edit(host_id, vm_id, params)
                await _save_state()
                await _publish(channel, host_id, QueueKind.RESPONSE, {
                    "id": task_id, "function": fn, "status": "succeeded", "progress": 100,
                    "result": "ok", "vm_id": vm_id,
                    "vm_status": vm_status, "finished_at": _now(),
                })
                return

            if fn == "vm_export_template":
                src = next((v for v in hs["vms"] if v["id"] == vm_id), None)
                tpl = {
                    "id": str(uuid.uuid4()),
                    "name": params.get("template_name") or "TEMPLATE",
                    "path": f"D:\\HyperV\\TEMPLATES\\{params.get('template_name') or 'TEMPLATE'}",
                    "sizeBytes": 18 * GiB, "diskSizeBytes": 6 * GiB,
                    "notes": params.get("notes") or None,
                    "cpuCount": (src or {}).get("vcpu") or 2,
                    "memoryMb": (src or {}).get("memoryMb") or 4096,
                    "guestOs": None, "createdAt": _now(),
                }
                hs.setdefault("templates", []).append(tpl)
                await _save_state()
                await _publish(channel, host_id, QueueKind.RESPONSE, {
                    "id": task_id, "function": fn, "status": "succeeded", "progress": 100,
                    "result": "exported", "vm_id": vm_id, "template": tpl,
                    "finished_at": _now(),
                })
                return

            final_state = {
                "vm_start": "Running",
                "vm_restart": "Running",
                "vm_stop": "Off",
                "vm_shutdown": "Off",
                "vm_pause": "Paused",
                "vm_delete": "Off",
            }.get(fn)
            _apply_action(host_id, vm_id, fn, final_state)
            await _save_state()
            await _publish(channel, host_id, QueueKind.RESPONSE, {
                "id": task_id, "function": fn, "status": "succeeded", "progress": 100,
                "result": "ok", "vm_id": vm_id, "vm_state": final_state,
                "finished_at": _now(),
            })

    await queues[QueueKind.REQUEST].consume(handle_request)

    while True:
        await _publish(channel, host_id, QueueKind.AGENT_STATUS, {
            "version": "0.4.1", "vm_refresh_interval": 180,
            "host_refresh_interval": 600, "reported_at": _now(),
            "hypervisor": "hyperv", "agent_type": "ovc-agent-hyperv",
            "hostname": name,
            "fqdn": f"{name}.lab.local",
            # management/primary IP (10.20.0.0/24) - not the vmotion net (10.99.0.0/24)
            "ip": f"10.20.0.{(hash(host_id) % 200) + 10}",
        })
        await _publish(channel, host_id, QueueKind.HOST_INVENTORY, {
            "reported_at": _now(), "hardware": hs["hardware"],
        })
        await _publish(channel, host_id, QueueKind.VM_INVENTORY, {
            "reported_at": _now(), "vms": hs["vms"],
        })
        await _publish(channel, host_id, QueueKind.TEMPLATE_INVENTORY, {
            "reported_at": _now(), "items": hs["templates"],
        })
        await _publish(channel, host_id, QueueKind.ISO_INVENTORY, {
            "reported_at": _now(), "items": hs["isos"],
        })
        _mem_total = 512 * GiB
        _mem_pct = round(random.uniform(30, 90), 1)
        await _publish(channel, host_id, QueueKind.HOST_METRICS, {
            "reported_at": _now(),
            "cpuPercent": round(random.uniform(5, 85), 1),
            "memPercent": _mem_pct,
            "memUsedBytes": int(_mem_total * _mem_pct / 100),
            "memTotalBytes": _mem_total,
            "diskLatencyMs": round(random.uniform(0.2, 8.0), 2),
            "netRxBps": random.randint(10_000, 5_000_000),
            "netTxBps": random.randint(10_000, 5_000_000),
            "disks": [
                {
                    "name": f"{i} {d}",
                    "readLatencyMs": round(random.uniform(0.1, 6.0), 2),
                    "writeLatencyMs": round(random.uniform(0.1, 9.0), 2),
                    "queueLength": round(random.uniform(0, 3), 2),
                }
                for i, d in enumerate(("C:", "D:"))
            ],
            "net": [
                {
                    "name": "Team-Prod",
                    "rxBps": random.randint(10_000, 4_000_000),
                    "txBps": random.randint(10_000, 4_000_000),
                }
            ],
        })
        await _publish(channel, host_id, QueueKind.VM_METRICS, {
            "reported_at": _now(),
            "vms": [
                {
                    "id": vm["id"],
                    "cpuPercent": round(random.uniform(0, 60), 1),
                    "memBytes": random.randint(1, 8) * 1024**3,
                    "diskBytes": random.randint(10, 120) * 1024**3,
                    "netRxBytes": random.randint(0, 500_000_000),
                    "netTxBytes": random.randint(0, 500_000_000),
                }
                for vm in hs["vms"]
                if vm.get("metricsEnabled")
            ],
        })
        await asyncio.sleep(20)


async def main() -> None:
    configure_logging()
    async with session_scope() as db:
        # the agent addresses its queues by the host short_id
        hosts = [
            (h.short_id, h.name)
            for h in (await db.execute(select(Host))).scalars().all()
        ]
    if not hosts:
        log.error("no hosts in DB - create one first: POST /api/hosts {name}")
        return

    _load_state()
    for hid, name in hosts:
        _host_state(hid, name)
    await _save_state()

    connection = await aio_pika.connect_robust(_settings.rabbitmq_url)
    log.info("fake agent serving %d host(s)", len(hosts))
    await asyncio.gather(*[_serve_host(connection, hid, name) for hid, name in hosts])


if __name__ == "__main__":
    asyncio.run(main())

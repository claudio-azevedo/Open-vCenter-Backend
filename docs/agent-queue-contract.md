# Agent queue contract

Everything the backend puts on a host agent's RabbitMQ queues, and everything it
expects back. The agent (`ovc-agent-hyperv`) is the other side of this contract;
the Python source of truth is `app/messaging/` + `app/services/{vms,inventory}.py`.

- **camelCase vs snake_case:** request/response envelopes and the `*_metrics` /
  `agent_status` payloads use **snake_case** top-level keys. Inventory _item_
  objects (each VM, template, ISO, hardware block) use **camelCase**, mirroring the
  frontend contract (`../ovc-frontend/docs/api-contract.md`).
- All messages are JSON, `content_type: application/json`, `delivery_mode: persistent`.
- Timestamps are ISO-8601 UTC (`2026-08-29T15:04:05.123456+00:00`). `Z` suffix is accepted.

---

## Queues

Per host (`<hostid>` = the host **short id** - 10-char `[A-Za-z0-9]`, `Host.short_id`,
also written as `host_id` in the agent `config.ini`; **not** the backend's UUID
primary key), all `durable: true`:

| Queue                         | Direction       | Type                          | Purpose                          |
| ----------------------------- | --------------- | ----------------------------- | -------------------------------- |
| `<hostid>.request`            | backend → agent | plain                         | task requests (this doc, Part 1) |
| `<hostid>.response`           | agent → backend | plain                         | task progress + result (Part 1)  |
| `<hostid>.agent_status`       | agent → backend | last-value (`x-max-length=1`) | heartbeat (Part 2)               |
| `<hostid>.vm_inventory`       | agent → backend | last-value                    | full VM list (Part 2)            |
| `<hostid>.host_inventory`     | agent → backend | last-value                    | host hardware (Part 2)           |
| `<hostid>.template_inventory` | agent → backend | last-value                    | VM export templates (Part 2)     |
| `<hostid>.iso_inventory`      | agent → backend | last-value                    | ISO library (Part 2)             |
| `<hostid>.vm_metrics`         | agent → backend | plain (short time-series)     | quick VM metrics (Part 2)        |
| `<hostid>.host_metrics`       | agent → backend | plain (short time-series)     | quick host metrics (Part 2)      |

The per-agent RabbitMQ user is scoped to exactly these queues for `configure` and
`read` (`app/messaging/queues.py::agent_permission_pattern`). Its `write` grant is
those queues **plus the default exchange** (`amq.default` / `""`) -
`agent_write_pattern` - because the agent publishes with the default exchange and
the queue name as routing key.

---

## Part 1 - Task requests (`<hostid>.request` → `<hostid>.response`)

### Request envelope

Written by `app/messaging/publisher.py::publish_request`. The AMQP message
`correlation_id` is set to `id`.

```json
{
  "id": "task_9f3c1a2b8d7e6f5a4b3c2d1e",
  "function": "vm_start",
  "params": { "vm_id": "b747349c-c90b-4126-bc2d-ef8ceb3444c4" },
  "requested_by": "operator@ovc.local",
  "issued_at": "2026-08-29T15:04:05.123456+00:00"
}
```

| Field          | Notes                                                                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`           | `Task.id`. Echo it back verbatim as `AgentResponse.id` - the backend matches on this, **not** on `function`.                                            |
| `function`     | one of the values in the table below                                                                                                                    |
| `params`       | `{ "vm_id": "<hyper-v guid>" }` for every request that targets an existing VM. The one exception is `vm_create` (no VM exists yet) - see its row below. |
| `requested_by` | the caller's email (display/audit only)                                                                                                                 |
| `issued_at`    | when the backend enqueued it                                                                                                                            |

### Functions the backend posts

Most are triggered by `POST /api/vms/{id}/actions/{action}` (or `DELETE /api/vms/{id}`)
→ `app/services/vms.py::request_vm_action`. `vm_create` (`POST /api/vms`) and
`vm_clone` (`POST /api/vms/clone`) each insert a placeholder `Vm` row first -
`request_vm_create` / `request_vm_clone`. There are **no** host-level requests
from the backend yet.

`POST /actions/{action}` accepts an optional body `{ "params": { … } }`; those
keys are forwarded verbatim as extra `AgentRequest.params` alongside `vm_id` (the
agent contract below defines the keys per function). The wire has no nested
object - `vm_id`, `action`, and the per-action keys all sit flat on `params`.

| `function`           | REST action               | Optimistic state the backend sets | Expected effect on the VM                                                                                                                                                                                      |
| -------------------- | ------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vm_start`           | `actions/start`           | `Starting`                        | VM running (also resumes a paused VM)                                                                                                                                                                          |
| `vm_stop`            | `actions/stop`            | `Stopping`                        | hard power-off                                                                                                                                                                                                 |
| `vm_shutdown`        | `actions/shutdown`        | `Stopping`                        | guest-OS shutdown                                                                                                                                                                                              |
| `vm_restart`         | `actions/restart`         | `Restarting`                      | VM restarted, running                                                                                                                                                                                          |
| `vm_pause`           | `actions/pause`           | `Pausing`                         | `Suspend-VM` on the VM (a `vm_resume` function runs `Resume-VM`)                                                                                                                                               |
| `vm_delete`          | `DELETE /vms/{id}`        | `Deleting`                        | VM removed from the host. Params: `remove_files` (bool, default `false`) - when `true` the agent also deletes the VM's folder/files from disk after `Remove-VM`. `DELETE /vms/{id}?remove_files=true` sets it. |
| `vm_enable_metrics`  | `actions/enable_metrics`  | _(none - power state untouched)_  | `Enable-VMResourceMetering` on the VM                                                                                                                                                                          |
| `vm_disable_metrics` | `actions/disable_metrics` | _(none)_                          | `Disable-VMResourceMetering` on the VM                                                                                                                                                                         |
| `vm_create`          | `POST /vms`               | `Unknown` (placeholder row)       | new VM created on the host - **stub on `ovc-agent-hyperv` today**                                                                                                                                              |

#### `vm_create` - creating a VM from scratch

`POST /api/vms` inserts a placeholder `Vm` row (`vmUuid = null`), then posts one
request whose `params` do **not** carry `vm_id`:

```jsonc
{
  "name": "SRV-APP-01",
  "destination_storage": "", // "" -> host default VM path; else "E:\\" or "C:\\ClusterStorage\\Volume2"
  "os": "windows",
  "firmware": "UEFI", // "BIOS" -> Generation 1, "UEFI" -> Generation 2
  "cpu_count": 2,
  "memory_mb": 4096, // startup RAM
  "memory_dynamic": false, // true -> Set-VMMemory -DynamicMemoryEnabled $true
  "memory_min_mb": null,
  "memory_max_mb": null, // dynamic only; null -> agent default (512 MB / 4x startup)
  "nested_virtualization": false,
  "vlan_id": 100,
  "switch_name": "vSwitch-Prod", // null -> agent picks the first vSwitch
  "dvd": "D:\\ISOS\\ws2022.iso",
  "ha_enabled": false,
  "cluster": null,
  "start_now": false,
  "disks": [{ "name": "OS", "type": "Fixed", "size_gb": 60 }], // agent builds "<VMFolder>\Virtual Hard Disks\<name>-OS.vhdx"
}
```

The backend sends only `destination_storage` - the **target volume/CSV**, never a
fully-qualified path. The **agent** resolves the VM's own folder (`resolveVMFolder`):

| `destination_storage`                                      | VM folder                                                    |
| ---------------------------------------------------------- | ------------------------------------------------------------ |
| `""` or same volume as `Get-VMHost.VirtualMachinePath`     | `<default VM path>\<name>` (e.g. `D:\HyperV\VMS\SRV-APP-01`) |
| a Cluster Shared Volume (`C:\ClusterStorage\VolumeN`)      | `<CSV root>\VMS\<name>`                                      |
| another local volume (a configured `aditional_vm_storage`) | `<root>\VMS\<name>` (e.g. `E:\HyperV\VMS\SRV-APP-01`)        |

The agent pre-creates `Snapshots` and `Virtual Hard Disks` under that folder and
passes its **parent** to `New-VM -Path` (Hyper-V appends the VM name and owns
`Virtual Machines`), so the config, VHDs, snapshots and smart-paging file all
live under `<VM folder>`. A VM is never created at the root of the configured VM
path, and the folder is never double-nested (`…\<name>\<name>`).

The agent must reply with the **new** `vm_id` (the Hyper-V GUID it just created)
and `vm_state`. On `succeeded` the backend stamps `vmUuid` + state onto the
placeholder row; on `failed` it deletes the placeholder.

#### `vm_edit` - `actions/edit` - **implemented** by `ovc-agent-hyperv`

Every key is optional; only the ones present are applied. The backend passes
`params` straight through (plus `vm_id`). On success the agent returns the fresh
`vm_status` so the backend updates the row immediately.

```jsonc
{
  "cpu_count": 4,
  "memory_mb": 8192, // startup RAM
  "memory_dynamic": true, // switch static <-> dynamic memory
  "memory_min_mb": 2048, // dynamic only; omitted keeps the VM's current floor
  "memory_max_mb": 16384, // dynamic only; omitted keeps the VM's current ceiling
  "notes": "…",
  "nested_virtualization": true,
  "secure_boot": true,
  "secure_boot_template": "Windows", // Windows | Linux | Others
  "automatic_start": "StartIfRunning",
  "automatic_start_delay": 30, // Nothing | StartIfRunning | Start
  "automatic_stop": "ShutDown", // TurnOff | ShutDown | Save

  "add_disk": [
    { "path": "…\\VM-DATA.vhdx", "type": "Dynamic", "size_gb": 100 },
  ],
  "edit_disk": [{ "path": "…\\VM-OS.vhdx", "size_gb": 80 }], // grow only
  "remove_disk": [{ "path": "…\\VM-DATA.vhdx", "delete_from_disk": false }],

  "add_nic": [{ "name": "Net 2", "vlan_id": 0, "switch_name": "vSwitch-Prod" }],
  "edit_nic": [
    {
      "nic_id": "<guid>",
      "name": "Net 1",
      "vlan_id": 100,
      "switch_name": "vSwitch-DMZ",
    },
  ],
  "remove_nic": [{ "nic_id": "<guid>" }],
}
```

- `switch_name` (on `add_nic` / `edit_nic`) - empty / omitted ⇒ the agent uses the
  first virtual switch on the host; a name that doesn't exist on the host is an error.
- Disk ops are rejected while the VM has snapshots. `edit_disk` can only grow a VHDX.
- The `memory_*` keys build a single `Set-VMMemory` call: any omitted field keeps
  the VM's live value, and the dynamic min/max are clamped so min <= startup <= max.
- Only `cpu_count` / the `memory_*` keys / `secure_boot` / `secure_boot_template` /
  `nested_virtualization` require the VM to be **Off**. Disk ops, NIC ops, notes
  and the start/stop policy apply while the VM is running (subject to Hyper-V's own
  hot-add rules - e.g. Gen 1 / IDE disks can't be hot-attached).

#### Management functions - **stubs** (agent not implemented yet)

The backend queues the task and publishes the request; with no agent handler the
worker's timeout sweeper flips the task to `timeout` after `task_timeout_seconds`.
None set an optimistic state. `params` keys are the agent's to define.

| `function`                             | REST action                                            | `params` keys (suggested)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| -------------------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vm_rename`                            | `actions/rename`                                       | `new_name`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `vm_migrate`                           | `actions/migrate`                                      | `target_host`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `vm_move`                              | `actions/move_storage`                                 | `destination_storage`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `vm_startup_change`                    | `actions/startup_change`                               | `automatic_start`, `automatic_start_delay`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `mount_dvd`                            | `actions/mount_dvd`                                    | `path`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `eject_dvd`                            | `actions/eject_dvd`                                    | -                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `enable_ha` / `disable_ha`             | `actions/enable_ha` / `actions/disable_ha`             | -                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `vm_export_template`                   | `actions/export_template`                              | `template_name`, `notes?`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `notes_edit`                           | `actions/notes_edit`                                   | `notes`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `refresh_status`                       | `actions/refresh`                                      | - (backend adds `vm_id`) - **implemented** by `ovc-agent-hyperv`; read-only "force refresh" of one VM. Queued **without** the VM lock (never blocks / is blocked by a mutating task). The agent re-inventories the VM and returns it as `vm_status`, which the backend folds into the row like any post-task snapshot.                                                                                                                                                                                                                          |
| `snapshot_create`                      | `actions/snapshot_create`                              | `name`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `snapshot_remove` / `snapshot_restore` | `actions/snapshot_remove` / `actions/snapshot_restore` | `snapshot_id`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `vm_clone`                             | `POST /vms/clone`                                      | `clone_type` (`imported`\|`exported`), `name`, `destination_storage`, `cpu_count`, `memory_mb`, `notes`, `vlan_id`, `nested_virtualization`, `ha_enabled`, `start_now`, plus `source_vm_name` (imported) **or** `source_export_path` + `expand_disks` (exported; converts the deployed VM's Dynamic disks to Fixed after import - on by default, `false` keeps them Dynamic). The agent resolves `path` and the imported-clone scratch `export_folder_root` itself (same `resolveVMFolder` rules as `vm_create`), so the backend sends neither. |

#### `host_update_agent` - `POST /hosts/{id}/actions/update-agent` - **implemented** by `ovc-agent-hyperv`

The agent self-upgrade. `target_type` is `host`. `params`:

| key               | notes                                                                                                                                                                                                                                 |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `download_url`    | plain-HTTP GET, no auth header, redirects followed, 2xx required. Either an S3 presigned URL (`OVC_AGENT_STORAGE=s3`) or `{OVC_PUBLIC_BASE_URL}/api/agent-binaries/{id}/download?exp=…&token=…` (local mode; short-lived HMAC token). |
| `checksum_sha256` | the agent verifies the download against this                                                                                                                                                                                          |
| `version`         | target `AgentVersion`. The agent **rejects** the upgrade if this equals its running version - the backend pre-checks the same (409 `AGENT_ALREADY_CURRENT`).                                                                          |

The backend refuses a second concurrent upgrade for the same host (409
`AGENT_UPGRADE_IN_PROGRESS`) and refuses when the host agent is offline (409
`HOST_OFFLINE`).

**Two-phase response.** The _old_ agent publishes `running` progress up to `99`
("Agent binary downloaded, applying upgrade…"), then goes quiet while the service
process swaps the binary and restarts. The _new_ agent, on startup, publishes the
single terminal response for the original `id` - `succeeded` with
`result: { version, old_version, upgraded: true }`, or `failed` on rollback. The
worker stamps `Host.agent_version` from a successful `result.version` immediately.
Task timeout is 1800s (`app/task_timeouts.py`) to cover the whole window; if the
new agent never confirms, the sweeper marks the task `timeout` and the next
`agent_status` reconciles the version.

### Response envelope

Written to `<hostid>.response`, consumed by `app/services/inventory.py::apply_response`.

```json
{
  "id": "task_9f3c1a2b8d7e6f5a4b3c2d1e",
  "function": "vm_start",
  "status": "succeeded",
  "progress": 100,
  "result": "started",
  "error": null,
  "vm_id": "b747349c-c90b-4126-bc2d-ef8ceb3444c4",
  "vm_state": "Running",
  "finished_at": "2026-08-29T15:04:12.900000+00:00"
}
```

| Field                | Required | How the backend uses it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| -------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                 | yes      | matches the `Task`. Current agents don't send a response for their periodic refresh jobs, but an unknown id starting with `refresh-` is still dropped silently (DEBUG) for older agents - the payload always arrives on the dedicated queue anyway. Any other unknown id logs a WARNING.                                                                                                                                                                                                                                                                                                                                                       |
| `function`           | yes      | echoed only - not used for routing                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `status`             | yes      | `"running"` \| `"succeeded"` \| `"failed"`. Anything other than `running`/`succeeded` is treated as `failed`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `progress`           | no       | `0–100` int. Applied only while the task is `queued`/`running`. On success the backend forces `100`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `progress_message`   | no       | human-readable step text (`"Exporting VM (45%)"`, `"Creating disk 2/3: …"`). Stored in `Task.progress_message` while `running`; **cleared** on the terminal response. Surfaced by the UI as a live "Details" line.                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `result`             | no       | **persisted only when it is a string** (`Task.result`). An object is accepted on the wire but dropped - except a `{ "vms": [...] }` wrapper, which is applied like `vm_status` (legacy path).                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `error`              | no       | stored in `Task.error` on a failed task                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `vm_id` + `vm_state` | no       | on a **terminal** response, the backend immediately updates the VM row + fast-cache state to `vm_state` (so the UI doesn't wait for the next `vm_inventory`). Send both or neither. Ignored when `vm_status` is present.                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `vm_status`          | no       | the **fresh, full VM object** after a VM-mutating task (one normalized `VMInfo` - same per-VM shape as `vm_inventory`, or a `{ "vms": [...] }` wrapper). On a terminal `succeeded` response the backend applies it to the matching row (by `id` = Hyper-V GUID, scoped to the host) as a **partial** update - only the keys present are written, so a lean payload never nulls out fields a full inventory filled in. This is how notes/DVD/HA/auto-start/snapshot changes reach the DB immediately in the worker-process model (the agent can't publish a follow-up `vm_inventory`). Unknown VM → skipped, waits for the next full inventory. |
| `template`           | no       | on a terminal `succeeded` **`vm_export_template`** response, one `template_inventory` item (`{id,name,path,sizeBytes,diskSizeBytes,notes,cpuCount,memoryMb,guestOs,createdAt}`) - the backend upserts the `Template` row by `id` right away instead of waiting for the next `template_inventory` (which reconciles it regardless).                                                                                                                                                                                                                                                                                                             |
| `finished_at`        | no       | stored as `Task.finished_at`; defaults to receipt time                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |

Regardless of the field-by-field handling above, the backend also stores the
**whole envelope verbatim** in `Task.response_payload` (JSONB) on every
`apply_response` call - the latest one wins, so a finished task carries its
terminal response. The matching `AgentRequest` it published is kept in
`Task.request_payload`. Both are returned only by `GET /tasks/:id`
(`TaskDetailOut`) and power the task **Details** dialog in the UI.

`vm_state` must be one of:
`Running, Off, Paused, Saved, Starting, Stopping, Saving, Pausing, Resuming, Restarting, Deleting, Unknown`.

### Response lifecycle

1. **(optional) progress** - zero or more, while the work runs:

   ```json
   {
     "id": "task_9f3c…",
     "function": "vm_restart",
     "status": "running",
     "progress": 40,
     "progress_message": "Restarting VM",
     "result": null,
     "error": null,
     "vm_id": null,
     "vm_state": null,
     "finished_at": null
   }
   ```

2. **exactly one terminal** - `succeeded` or `failed`:

   _success (state-changing action)_ - attach `vm_status` (the fresh full VM) so
   the row updates now; `vm_id`/`vm_state` are a fallback for older agents:

   ```json
   {
     "id": "task_9f3c…",
     "function": "vm_restart",
     "status": "succeeded",
     "progress": 100,
     "result": "restarted",
     "error": null,
     "vm_id": "b747349c-c90b-4126-bc2d-ef8ceb3444c4",
     "vm_state": "Running",
     "vm_status": {
       "id": "b747349c-…",
       "name": "web-01",
       "state": "Running",
       "notes": "…",
       "…": "…"
     },
     "finished_at": "2026-08-29T15:05:01+00:00"
   }
   ```

   _success (`vm_delete`)_ - omit `vm_state`; the backend deletes the VM row by the
   task's target when a `vm_delete` task succeeds with no `vm_state`:

   ```json
   {
     "id": "task_del…",
     "function": "vm_delete",
     "status": "succeeded",
     "progress": 100,
     "result": "deleted",
     "error": null,
     "vm_id": null,
     "vm_state": null,
     "finished_at": "2026-08-29T15:06:00+00:00"
   }
   ```

   The backend also tombstones the VM's Hyper-V GUID for ~15 min so a
   `vm_inventory` snapshot the agent captured _before_ the delete (still on the
   last-value queue) can't re-create the row when consumed out of order. The
   next fresh snapshot that omits the GUID clears the tombstone.

   _success (`vm_enable_metrics` / `vm_disable_metrics`)_ - no power-state change;
   attach `vm_status` (carrying the fresh `metricsEnabled`) so it applies now,
   else it waits for the next `vm_inventory`:

   ```json
   {
     "id": "task_met…",
     "function": "vm_enable_metrics",
     "status": "succeeded",
     "progress": 100,
     "result": "metering enabled",
     "error": null,
     "vm_id": null,
     "vm_state": null,
     "vm_status": { "id": "b747349c-…", "metricsEnabled": true, "…": "…" },
     "finished_at": "2026-08-29T15:07:00+00:00"
   }
   ```

   _failure:_

   ```json
   {
     "id": "task_9f3c…",
     "function": "vm_start",
     "status": "failed",
     "progress": null,
     "result": null,
     "error": "Start-VM: 'web-01' failed to start: not enough memory",
     "vm_id": null,
     "vm_state": null,
     "finished_at": "2026-08-29T15:04:20+00:00"
   }
   ```

If no terminal response arrives within `OVC_TASK_TIMEOUT_SECONDS` (default 300) the
worker marks the task `timeout` on its own (`app/worker/main.py::_timeout_sweeper`).
Some kinds get a longer grace (`_LONG_TASK_TIMEOUTS`): `vm_shutdown` 900s;
`vm_move` / `vm_rename` / `vm_export_template` / `vm_batch_*` / `vm_clone` /
`vm_edit` 2100s - each kept above the agent's own hard kill so the backend never
times a task out while the agent is still working.

**VM lock.** When the backend queues a mutating VM task it takes a Valkey lock
(`vm:lock:<vm_id>`, TTL = task timeout + 60s); a second operation on that VM is
rejected with HTTP 409 `VM_LOCKED` until the task reaches a terminal state
(`apply_response` / the timeout sweeper release it; the TTL is the crash backstop).
The agent is not involved - it never sees the lock. Admin-only recovery endpoints:
`GET /vm-locks`, `DELETE /vm-locks`, `DELETE /vm-locks/{vm_id}`.

---

## Part 2 - Agent-published state queues

The backend **never requests** these - the agent publishes them on its own
schedule (`refresh_interval_vms` / `refresh_interval_host` / `metrics_interval` in
the agent's `config.ini`). The worker subscribes to all of them
(`app/worker/consumers.py`).

### `agent_status` - heartbeat

Consumed by `apply_agent_status`. Drives `Host.agent_version`,
`agent_refresh_vm`, `agent_refresh_host`, `agent_last_seen` (→ the host's
`online` flag; a host is offline once this is older than
`OVC_AGENT_OFFLINE_AFTER_SECONDS`, default 120), plus `Host.fqdn` (from
`fqdn`, falling back to `hostname`) and `Host.ip_address` (from `ip` /
`ipAddress` / `primaryIp`) - these are **agent-resolved**, not asked for at
registration.

```json
{
  "version": "1.1.10",
  "vm_refresh_interval": 180,
  "host_refresh_interval": 600,
  "reported_at": "2026-08-29T15:04:00+00:00",
  "hypervisor": "hyperv",
  "agent_type": "ovc-agent-hyperv",
  "hostname": "HV-NODE-01",
  "fqdn": "HV-NODE-01.corp.example.com",
  "ip": "10.20.0.11",
  "os_version": "Microsoft Windows Server 2022 Datacenter",
  "uptime_seconds": 864000
}
```

`version`, `vm_refresh_interval`, `host_refresh_interval`, `reported_at`,
`hypervisor`, `fqdn`/`hostname` (→ `Host.fqdn`) and `ip` (→ `Host.ip_address`)
are read today (camelCase `vmRefreshInterval` / `hostRefreshInterval` also
accepted). The rest is forwarded for future use - keep sending it.

**`ip` is the host's management / primary address** - the NIC used to reach the
site network, never a dedicated vMotion / live-migration / storage link. The
agent does **not** assume Internet access; it resolves the address by:

1. ranking the Up adapters that hold an IPv4 by _(has default gateway)_ then
   _(has IPv4 DNS servers)_ then _(lowest route + interface metric)_
   (`Get-NetIPConfiguration`), and
2. route-looking-up each configured DNS server - the source address the OS would
   use to reach its own DNS is, by construction, the management IP (a connected
   UDP socket performs no I/O, so this works on an air-gapped host).

Isolated back-end links carry neither a default gateway nor DNS servers, so both
steps skip them.

### `vm_inventory` - full VM list (authoritative)

Consumed by `apply_vm_inventory`, which runs `services/inventory.py::normalize_vm`
first. **Replace-all semantics:** any VM in the DB for this host not in `vms[]` is
deleted. `folderId` is backend-managed and ignored if sent.

The canonical per-VM shape is below. The Hyper-V agent's `VMInfo` uses flatter
keys and `normalize_vm` maps them: `cpu.vcpus`→`vcpu`, `memoryMb` (MiB)→
`memoryBytes`, `memoryConsumedMb`→`memoryDemandBytes`, `cpuUsagePct`→
`cpuUsagePercent`, `vhds[{diskId,sizeGb,format,usedBytes}]`→`disks[…]`,
`nics[].networkId`→`switchName` and `ipAddresses` string→list, `automaticStart*`
→`autoStart*`, `path`→`configPath`, `dvd`→`dvdPath`, `ha`→`highlyAvailable`,
`snapshots[].creationTime`→`createdAt`, `creationTime`→`createdAt`. A payload
already in canonical form passes through.

```json
{
  "reported_at": "2026-08-29T15:04:00+00:00",
  "vms": [
    {
      "id": "b747349c-c90b-4126-bc2d-ef8ceb3444c4",
      "name": "web-01",
      "state": "Running",
      "firmware": "UEFI",
      "uptimeSec": 512340,
      "vcpu": 4,
      "cpuUsagePercent": 5,
      "metricsEnabled": true,
      "memoryBytes": 8589934592,
      "memoryMinBytes": 2147483648,
      "memoryMaxBytes": 17179869184,
      "memoryDynamic": true,
      "memoryDemandBytes": 4294967296,
      "secureBoot": true,
      "secureBootTemplate": "Windows",
      "nestedVirtualization": false,
      "autoStartAction": "StartIfRunning",
      "autoStartDelaySec": 0,
      "autoStopAction": "Save",
      "configPath": "D:\\VMs\\web-01",
      "dvdPath": null,
      "highlyAvailable": true,
      "disks": [
        {
          "id": "scsi-0-0",
          "path": "D:\\VMs\\web-01\\disk0.vhdx",
          "controller": "SCSI 0:0",
          "sizeBytes": 137438953472,
          "usedBytes": 64424509440,
          "type": "Dynamic",
          "format": "VHDX"
        }
      ],
      "nics": [
        {
          "id": "net-0",
          "name": "Network Adapter",
          "switchName": "vSwitch-LAN",
          "vlanId": 0,
          "macAddress": "00:15:5D:01:0A:0B",
          "ipAddresses": ["10.0.6.21"],
          "connected": true
        }
      ],
      "snapshots": [
        {
          "id": "snap-1",
          "name": "before-update",
          "parentId": null,
          "type": "Standard",
          "createdAt": "2026-06-16T08:00:00+00:00"
        }
      ],
      "notes": "prod web frontend",
      "createdAt": "2025-11-02T09:12:00+00:00"
    }
  ]
}
```

| Item field                                          | Consumed as                                                                                                                                                                          | Default if missing                  |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------- |
| `id`                                                | `Vm.vm_uuid` (the Hyper-V VM GUID). Matched **per cluster** (or per standalone host) to an existing row; the backend's own `Vm.id` is a separate UUID it never exposes to the agent. | **required**                        |
| `name`                                              | `Vm.name`                                                                                                                                                                            | keeps existing / falls back to `id` |
| `state`                                             | `Vm.state` + fast cache                                                                                                                                                              | `"Unknown"`                         |
| `firmware`                                          | `Vm.firmware` - `"BIOS"` \| `"UEFI"` (legacy `generation` int: 1→BIOS else UEFI)                                                                                                     | `"UEFI"`                            |
| `uptimeSec`                                         | `Vm.uptime_sec`                                                                                                                                                                      | `null`                              |
| `vcpu`                                              | `Vm.vcpu`                                                                                                                                                                            | `1`                                 |
| `cpuUsagePercent` (agent `cpuUsagePct`)             | `Vm.cpu_usage_percent` - a simple hypervisor average, **not** a time-series                                                                                                          | `null`                              |
| `metricsEnabled`                                    | `Vm.metrics_enabled` - gates whether `vm_metrics` rows are written for this VM                                                                                                       | keeps existing (omit → unchanged)   |
| `memoryBytes` (agent `memoryMb`, MiB)               | `Vm.memory_bytes`                                                                                                                                                                    | `0`                                 |
| `memoryMinBytes` / `memoryMaxBytes`                 | `Vm.memory_min_bytes` / `Vm.memory_max_bytes`                                                                                                                                        | `null`                              |
| `memoryDynamic`                                     | `Vm.memory_dynamic`                                                                                                                                                                  | `false`                             |
| `memoryDemandBytes` (agent `memoryConsumedMb`, MiB) | `Vm.memory_demand_bytes` - simple average, not time-series                                                                                                                           | `null`                              |
| `secureBoot` / `secureBootTemplate`                 | `Vm.secure_boot` / `Vm.secure_boot_template`                                                                                                                                         | `null`                              |
| `nestedVirtualization` (agent `nested`)             | `Vm.nested_virtualization`                                                                                                                                                           | `false`                             |
| `autoStartAction` (agent `automaticStart`)          | `Vm.auto_start_action`                                                                                                                                                               | `null`                              |
| `autoStartDelaySec` (agent `automaticStartDelay`)   | `Vm.auto_start_delay_sec`                                                                                                                                                            | `null`                              |
| `autoStopAction` (agent `automaticStop`)            | `Vm.auto_stop_action`                                                                                                                                                                | `null`                              |
| `configPath` (agent `path`)                         | `Vm.config_path`                                                                                                                                                                     | `null`                              |
| `dvdPath` (agent `dvd`)                             | `Vm.dvd_path`                                                                                                                                                                        | `null`                              |
| `highlyAvailable` (agent `ha`)                      | `Vm.highly_available`                                                                                                                                                                | `false`                             |
| `disks` (agent `vhds`)                              | `vm_disks` rows, rebuilt each refresh (incl. `usedBytes`)                                                                                                                            | `[]`                                |
| `nics`                                              | `vm_nics` rows, rebuilt each refresh                                                                                                                                                 | `[]`                                |
| `snapshots`                                         | `vm_snapshots` rows, rebuilt each refresh. Agent keys: `id`, `name`, `creationTime`, `snapshotType`, `parentSnapshotId`                                                              | `[]`                                |
| `notes`                                             | `Vm.notes`                                                                                                                                                                           | `null`                              |
| `createdAt`                                         | `Vm.vm_created_at`                                                                                                                                                                   | `null`                              |

### `host_inventory` - hardware

Consumed by `apply_host_inventory`: runs `services/inventory.py::normalize_hardware`
and stores the result in `Host.hardware` (JSONB). Current agents send the canonical
shape already (`HardwareInventoryResult.MarshalJSON` in the agent); `normalize_hardware`
passes it through and maps a legacy flat payload from an old agent. `_hardware_out`
re-normalizes on read, so old rows are repaired lazily.

**Canonical shape** (mirrors `../ovc-frontend/docs/api-contract.md` →
`HostHardwareInventory`):

```json
{
  "reported_at": "2026-08-29T15:00:00+00:00",
  "hardware": {
    "cpu": {
      "model": "Intel(R) Xeon(R) Gold 6338",
      "sockets": 2,
      "cores": 32,
      "logical": 64
    },
    "memoryBytes": 274877906944,
    "storage": [
      { "path": "C:\\", "totalBytes": 512000000000, "freeBytes": 210000000000 },
      {
        "path": "D:\\",
        "label": "VMs",
        "totalBytes": 8000000000000,
        "freeBytes": 3100000000000
      }
    ],
    "os": {
      "caption": "Microsoft Windows Server 2022 Datacenter",
      "version": "10.0.20348"
    },
    "network": [
      {
        "name": "Team-Prod",
        "description": "Intel X710 #1",
        "mac": "00-15-5D-01-0A-01",
        "speedBps": 25000000000,
        "connected": true
      }
    ],
    "vSwitches": [
      { "name": "vSwitch-Prod", "type": "External", "netAdapter": "Team-Prod" }
    ],
    "system": { "manufacturer": "Dell Inc.", "model": "PowerEdge R650" },
    "bootTime": "2026-08-25T09:14:03-03:00",
    "load": { "cpuPercent": 21, "memoryPercent": 47.3 },
    "cluster": {
      "clustered": true,
      "name": "HV-CLUSTER-01",
      "state": "Up",
      "nodes": ["HV-01", "HV-02"]
    },
    "hyperv": {
      "defaultVmPath": "D:\\HyperV\\VMS\\",
      "defaultVhdPath": "D:\\HyperV\\VHDS\\"
    }
  }
}
```

- **`network`** lists every physical host NIC - `connected` reflects link state
  (`Get-NetAdapter` `Status -eq 'Up'`); disconnected/disabled adapters are still listed.
- **`vSwitches`** lists the host's virtual switches (`name`, `type` =
  External/Internal/Private, `netAdapter` = physical uplink for External).
- **`system` / `load` / `cluster` / `hyperv`** are optional; omit or `null` when unknown.
- **`load`** is a snapshot from the last refresh (default every 10 min) - _not_ live
  metrics (those are `<hostid>.host_metrics`).
- **`cluster`** is the Windows **Failover Cluster**, distinct from the app-level
  `Host.cluster_id` grouping.
- The agent still gathers `totalVMs` / `runningVMs` / `uptimeDays` / `autoBalancer*`
  but does not send them.

### `template_inventory` / `iso_inventory` - image library

Consumed by `apply_template_inventory` / `apply_iso_inventory`. **Replace-all** for
this host on every message.

```json
{
  "reported_at": "2026-08-29T15:00:00+00:00",
  "items": [
    {
      "id": "tpl_win2022_base",
      "name": "Windows 2022 Base",
      "path": "D:\\Templates\\win2022-base.vhdx",
      "sizeBytes": 32212254720,
      "diskSizeBytes": 9184505856,
      "notes": "sysprepped, .NET 4.8",
      "cpuCount": 4,
      "memoryMb": 8192,
      "guestOs": "Windows Server 2022",
      "createdAt": "2026-01-10T00:00:00+00:00"
    }
  ]
}
```

`sizeBytes` is the **provisioned** size (sum of each disk's virtual/max size - the
space a deployed VM occupies once its disks are Fixed). `diskSizeBytes` is the
**real on-disk footprint** of the exported template folder; `vm_export_template`
converts the template's disks to Dynamic, so this is normally much smaller and is
what the "import to repository" gate should use. Both are captured at export time
into `ovc-template-metadata.json` (`size`) and recomputed each inventory cycle.
`notes` and `createdAt` come from that same metadata JSON (`notes` / `exported_at`,
set at export from the `vm_export_template` params) - empty for legacy templates.

ISO items are the same minus `guestOs` / `createdAt`:

```json
{
  "reported_at": "2026-08-29T15:00:00+00:00",
  "items": [
    {
      "id": "iso_win2022",
      "name": "SW_DVD9_Win_Server_2022.iso",
      "path": "D:\\ISOs\\win2022.iso",
      "sizeBytes": 5825380352
    }
  ]
}
```

`id` is optional - if omitted the backend generates one (`tpl_…` / `iso_…`), but a
stable id (e.g. a content hash) keeps rows steady across refreshes.

### `vm_metrics` - quick per-VM metrics

Consumed by `apply_vm_metrics`. **Append** one sample row per listed VM; the worker
keeps only the last `OVC_METRICS_RETENTION_SECONDS` (default 3600 s) and drops the
rest. Only include VMs that have Hyper-V resource metering enabled. The backend
**also** filters: a sample is stored only when `Vm.metrics_enabled` is true for
that VM (unknown or non-metered VMs are silently skipped - see the `skipped=`
count in the log line). Full payload is stored verbatim in `detail`.

For the non-time-series counters (a plain hypervisor average of CPU % and memory
demand), don't rely on `vm_metrics` - those ride on every `vm_inventory` as
`cpuUsagePercent` / `memoryDemandBytes` and are stored on the `Vm` row itself.

```json
{
  "reported_at": "2026-08-29T15:05:00+00:00",
  "vms": [
    {
      "id": "b747349c-c90b-4126-bc2d-ef8ceb3444c4",
      "cpuPercent": 12.5,
      "memBytes": 6442450944,
      "diskBytes": 64424509440,
      "netRxBytes": 184320000,
      "netTxBytes": 43520000
    }
  ]
}
```

| Field                       | Column                          | Notes                                |
| --------------------------- | ------------------------------- | ------------------------------------ |
| `id`                        | (row key → `vm_id`)             | must be a known VM GUID on this host |
| `cpuPercent`                | `cpu_percent`                   | best-effort; `null` allowed          |
| `memBytes`                  | `mem_bytes`                     |                                      |
| `diskBytes`                 | `disk_bytes`                    |                                      |
| `netRxBytes` / `netTxBytes` | `net_rx_bytes` / `net_tx_bytes` |                                      |

### `host_metrics` - quick host metrics

Consumed by `apply_host_metrics`. **Append** one sample; same last-hour retention.
Full payload stored in `detail`.

```json
{
  "reported_at": "2026-08-29T15:05:00+00:00",
  "cpuPercent": 37.0,
  "memPercent": 61.4,
  "diskLatencyMs": 1.8,
  "netRxBps": 12500000,
  "netTxBps": 3400000,
  "disks": [
    {
      "name": "0 C:",
      "readLatencyMs": 0.4,
      "writeLatencyMs": 1.1,
      "queueLength": 0.0
    }
  ],
  "net": [{ "name": "Ethernet 1", "rxBps": 12000000, "txBps": 3200000 }]
}
```

| Field                   | Column                                                      |
| ----------------------- | ----------------------------------------------------------- |
| `cpuPercent`            | `cpu_percent`                                               |
| `memPercent`            | `mem_percent`                                               |
| `diskLatencyMs`         | `disk_latency_ms` (avg across volumes)                      |
| `netRxBps` / `netTxBps` | `net_rx_bps` / `net_tx_bps`                                 |
| `disks[]` / `net[]`     | kept only inside `detail` (per-device breakdown for the UI) |

All scalar fields are nullable - send `null` (or omit) what you can't measure.

---

## Quick reference

```
backend ──> <hostid>.request     { id, function, params:{vm_id}, requested_by, issued_at }
agent   ──> <hostid>.response     { id, function, status, progress, progress_message, result, error, vm_id, vm_state, vm_status, template, finished_at }
                                    status: running (0..n)  →  succeeded | failed (exactly 1)

agent   ──> <hostid>.agent_status         { version, vm_refresh_interval, host_refresh_interval, reported_at, … }
agent   ──> <hostid>.vm_inventory         { reported_at, vms:[ {id,name,state,firmware,uptimeSec,vcpu,cpuUsagePercent,metricsEnabled,memoryBytes,memoryMin/MaxBytes,memoryDynamic,memoryDemandBytes,secureBoot(+Template),nestedVirtualization,autoStart/StopAction,autoStartDelaySec,configPath,dvdPath,highlyAvailable,disks,nics,snapshots,notes,createdAt} ] }
agent   ──> <hostid>.host_inventory       { reported_at, hardware:{cpu,memoryBytes,storage,os,network} }
agent   ──> <hostid>.template_inventory   { reported_at, items:[ {id,name,path,sizeBytes,diskSizeBytes,notes,cpuCount,memoryMb,guestOs,createdAt} ] }
agent   ──> <hostid>.iso_inventory        { reported_at, items:[ {id,name,path,sizeBytes} ] }
agent   ──> <hostid>.vm_metrics           { reported_at, vms:[ {id,cpuPercent,memBytes,diskBytes,netRxBytes,netTxBytes} ] }
agent   ──> <hostid>.host_metrics         { reported_at, cpuPercent, memPercent, diskLatencyMs, netRxBps, netTxBps, disks[], net[] }
```

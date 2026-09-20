# ovc-backend - context for LLMs

REST API + RabbitMQ worker for **Open vCenter**. Part of a three-service suite:

- **ovc-frontend** (React SSR) - the only REST client. Contract lives in
  `../ovc-frontend/docs/api-contract.md`; response shapes are **camelCase**.
- **ovc-backend** (this repo) - owns the schema in Postgres + the Valkey cache. Talks
  to agents **only** via RabbitMQ. Two processes: the API and the worker.
- **infra-containers** (`../infra-containers`) - the actual Postgres / Valkey /
  RabbitMQ / Keycloak containers (`./start-containers.sh`). This repo does **not**
  run them; `docker-compose.yml` here has only `api` + `worker` (+ `fake-agent`),
  reaching the infra via `host.docker.internal`. Creds: `ovc` / `ovc123`.
- **ovc-agent** (Go, Windows) - runs on each Hyper-V host. Consumes `<hostid>.request`,
  replies on `<hostid>.response`, periodically posts inventory queues.

## Conventions

All code, comments, identifiers, commit messages, docs and log strings are in
**English**, regardless of the language a contributor chats in.

## Hypervisors

Every `Host` and `Cluster` carries a `hypervisor` discriminator (`app/models/host.py`
`Hypervisor` StrEnum). **Today the only value is `hyperv`.** The plan is one agent
build per hypervisor (libvirt / KVM next); the RabbitMQ queue layout and the
request/response + inventory payloads are already hypervisor-agnostic, so a new agent
plugs in without contract changes. A cluster is hypervisor-homogeneous - `create_host`
/ `move_host` reject a host whose `hypervisor` differs from the cluster's. It is
first set from `POST /hosts` (default `hyperv`) and then confirmed by
`apply_agent_status` from the agent's `hypervisor` field.

## Two processes

| Process    | Entry                                    | Job                                                                                                                                                                                                                 |
| ---------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **API**    | `python -m app` / `uvicorn app.main:app` | serve `/api/*`; on startup, ensure every known host's RabbitMQ queues exist; publish agent requests; create `Task` rows                                                                                             |
| **Worker** | `python -m app.worker`                   | consume every host's `response` + 5 inventory + 2 metrics queues; update Postgres and the Valkey VM-state cache; time out stale tasks; prune quick-metrics older than `OVC_METRICS_RETENTION_SECONDS` (default 1 h) |

They share the `app` package (models, config, messaging, services).

## Stack

- **FastAPI** + **Pydantic v2** (`CamelModel` base → camelCase on the wire)
- **SQLAlchemy 2.0 async** + **asyncpg** + **Alembic** (async `env.py`)
- **aio-pika** for RabbitMQ, **redis-py** (asyncio) for Valkey
- Python **3.11+** (`from __future__ import annotations` everywhere)
- All settings via env, prefix `OVC_` (`app/config.py`)

## Layout

```
app/
  config.py            OVC_* settings (pydantic-settings)
  db.py                async engine + get_session (per-request) / session_scope (worker)
  cache.py             Valkey client; vm:state:<id> overlay helpers
  ids.py               new_id() -> str(uuid4). Every backend-generated id/FK is a
                       native Postgres UUID column (base.UUID_STR = Uuid(as_uuid=False),
                       so `str` in Python). template/iso ids stay agent-owned strings;
                       host_metrics/vm_metrics keep bigint autoincrement PKs.
                       new_host_short_id() -> 10 hex chars: Host.short_id, the
                       handle used in RabbitMQ queue names / agent user / config.ini
                       / logs (the UUID id stays the PK + FK target everywhere).
                       Vm.id is a backend UUID; the Hyper-V VM GUID lives in
                       Vm.vm_uuid (unique per cluster / per standalone host - the
                       same GUID may legitimately exist on unrelated hosts).
                       VM disks / nics are child tables (vm_disks, vm_nics),
                       rebuilt each vm_inventory by services.inventory._sync_*.
  models/              SQLAlchemy: Cluster, Host, Folder, Vm, Template, Iso, Task,
                       User, ScopeGrant. All datetime cols are timezone-aware.
                       Folder is scoped to exactly one of a cluster OR a standalone
                       host (CHECK constraint), flat (no nesting), app-managed.
  schemas/             CamelModel response schemas (…Out) + common error envelope
  messaging/
    queues.py          QueueKind enum, queue_name(), declare_host_queues(),
                       agent_permission_pattern() (configure/read = own queues) +
                       agent_write_pattern() (write = own queues + amq.default -
                       the agent publishes via the default exchange).
                       agent_status/*_inventory are "last value" queues
                       (x-max-length=1, x-overflow=drop-head). *_metrics are plain
                       durable queues - a short time-series, not last-value.
    rabbit.py          robust connection singleton
    protocol.py        AgentRequest / AgentResponse / *InventoryPayload shapes
    publisher.py       publish_request() -> writes JSON to <hostid>.request,
                       correlation_id = Task.id

  The full queue contract - every request the backend posts, every response /
  inventory / metrics payload the agent sends back, with JSON examples - is
  `docs/agent-queue-contract.md`.
  services/
    auth.py            get_or_create_user (SAVEPOINT-guarded - safe under the
                       first-request race on an empty DB); resolve_stub_user
    rbac.py            resolve_scope(user) -> VisibleScope (all | cluster/host/folder ids)
    hosts.py           create_host (+ provision queues), ensure_all_host_queues,
                       render_agent_config / agent_amqp_url (the "Setup Agent" config.ini -
                       host/port/vhost from OVC_AGENT_RABBITMQ_URL, falling back to
                       OVC_RABBITMQ_URL; set it whenever agents can't reach the broker via
                       the cluster-internal DNS OVC_RABBITMQ_URL points at, e.g. prod)
    organization.py    create/rename/delete_cluster (delete needs empty cluster),
                       create_host / move_host (cluster<->standalone) / delete_host
                       (cascades VMs+folders), create/rename/delete folder,
                       move_vm_to_folder (reachability checks)
    vms.py             request_vm_action() -> Task + optimistic state + publish.
                       enable_metrics/disable_metrics = no power-state change.
    inventory.py       apply_* : agent payloads -> DB + cache (worker side).
                       normalize_vm() / normalize_hardware() map the agent's
                       VMInfo / host hwinventory onto the backend shapes (agent
                       sends canonical hardware already; VMs still need mapping).
                       agent never touches folders or vm.folder_id.
                       apply_{host,vm}_metrics append a sample and prune the window.
    serializers.py     ORM row -> …Out schema (online/agent status, VM-state overlay).
                       _hardware_out normalizes stored hardware and never 500s.
  api/
    errors.py          ApiError + handlers -> { "error": { code, message, details } }.
                       A non-UUID id in the path (stale bookmark) → 404, not 500.
    deps.py            DbSession, CurrentUser, Scope dependencies
    routes/            health, auth(/me), clusters, hosts, folders, vms, tasks, images
  worker/
    main.py            connect, subscribe every host's consumed queues, rescan for
                       new hosts every 15s, timeout sweeper every 30s, metrics
                       retention sweeper every 300s
    consumers.py       dispatch a message by QueueKind to services.inventory

alembic/versions/0001_initial.py   full schema (autogenerated, UUID ids)
docs/agent-queue-contract.md       every request/response/payload on the agent queues
scripts/fake_agent.py              stand-in for ovc-agent (compose profile "demo"); needs hosts created first
docker-compose.yml                 api + worker (+ fake-agent) only - infra is ../infra-containers.
                                   DB starts EMPTY - no seed; register hosts via POST /api/hosts
```

## Async task model

```
POST /api/vms/:id/actions/start
   → RBAC check → create Task(status=queued) → set VM state "Starting" in DB + Valkey
   → publish AgentRequest to <hostid>.request (correlation_id = task.id)
   → 202 { task }
                                  agent runs vm_start, replies on <hostid>.response
worker consumes response:
   status "running"   → Task.status=running, started_at
   status "succeeded" → Task.status=succeeded, finished_at, progress=100,
                        VM state written to Valkey immediately (vm_state in the reply)
frontend polls GET /api/tasks/:id until terminal, then refetches /vms
```

The Valkey `vm:state:<id>` overlay is what makes the UI feel instant - read paths
(`/vms`, `/hosts/:id/vms`) prefer it over the DB row, which only catches up on the
next periodic `vm_inventory`.

## Auth

`OVC_AUTH_MODE=stub` (default): every request is the single user
`OVC_STUB_USER_EMAIL`, auto-provisioned with a `global` scope grant **and**
`OVC_ADMIN_ROLE` → sees everything (same variable, same value on the frontend -
set its `OVC_AUTH_MODE=stub` too).
`OVC_AUTH_MODE=oidc`: `app/api/deps.py::_verify_oidc` validates the
`Authorization: Bearer` JWT against `OVC_OIDC_JWKS_URL` (any OIDC provider; JWKS
cached `OVC_OIDC_JWKS_TTL_SECONDS`). Roles come from the token at the dot-path
`OVC_OIDC_ROLES_CLAIM` (default `resource_access.${client_id}.roles`, mirrors
ovc-frontend's `OIDC_ROLES_CLAIM`); users are upserted with **no** automatic grant.

`resolve_scope` (in `services/rbac.py`): a user carrying `OVC_ADMIN_ROLE`
(`ADMINISTRATOR`) gets `VisibleScope(all=True)` with no DB grant; everyone else
is limited to their `ScopeGrant`s. RBAC is enforced in the route handlers via the
`Scope` dependency (`VisibleScope.sees_host/…`); lists are filtered, direct reads 403.

## Gotchas

- Response JSON is camelCase - always use the `schemas/*Out` models, never dump ORM
  rows directly.
- Every model datetime column is `DateTime(timezone=True)`; asyncpg errors
  ("can't subtract offset-naive and offset-aware") if a bare `DateTime` sneaks in.
- The worker must survive a bad message - `handle_message` is wrapped; messages are
  `ack`'d (not requeued) even on error.
- Per-agent RabbitMQ user permissions: `configure`/`read` = `agent_permission_pattern`
  (own `<hostid>.*` queues); `write` = `agent_write_pattern` = that **plus**
  `amq.default` / `""`. The agent publishes every message through the **default
  exchange** (routing key = queue name), so without write on `amq.default` its
  consumer fails to start (`ACCESS_REFUSED ... exchange 'amq.default'`).
  `ensure_host_agent_user` (services/hosts.py) applies both; the startup preflight
  re-applies them for every host, so a backend restart repairs an existing user.
- Migrations run via `alembic upgrade head`; `alembic/env.py` reads the DSN from
  `app.config`, not `alembic.ini`.

## Not done yet

`vm_create` / `vm_edit`, RBAC admin endpoints (assigning `ScopeGrant`s),
SSE/WebSocket push, pagination, ETag/If-None-Match, tests.

## Agent binary distribution

`AgentBinary` rows (migration `0004`) catalogue uploaded `ovc-agent` builds; the
bytes live in the store picked by `OVC_AGENT_STORAGE` (`local` = a mounted dir,
`s3` = object storage) - `app/services/agent_store.py`. Admin API under
`/api/agent-binaries` (upload multipart, set active, delete, rollout).

Onboarding: `GET /api/hosts/:id/agent-install-url` (admin) mints a tokenized
`GET /api/hosts/:id/agent-install.ps1?exp=&token=` URL + a paste-ready elevated-
PowerShell one-liner (`iwr … | & …`). The `.ps1` route is auth'd by that HMAC
token (`OVC_AGENT_INSTALL_URL_TTL_SECONDS`, default 1h - the host running the
one-liner sends no cookie), not a session. The script itself creates
`C:\Program Files\ovc-agent`, downloads + checksums the active binary, writes
`config.ini`, installs the Windows service, opens the config in Notepad, and
prints how to `Start-Service ovc-agent` (it does **not** start the service).
Token signer: `sign_scope`/`verify_scope` in `app/services/agent_store.py`.

Upgrades: `POST /api/hosts/:id/actions/update-agent` queues `host_update_agent`
(offline hosts → 409 `HOST_OFFLINE`). Both onboarding and upgrades need a binary
download URL: local mode needs `OVC_PUBLIC_BASE_URL` (the URL a Hyper-V host uses
to fetch over plain HTTP, guarded by a short-lived HMAC token); s3 mode hands out
a presigned URL. See `docs/agent-queue-contract.md` → `host_update_agent`.

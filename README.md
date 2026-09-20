<h1 align="center">
  <a href="https://openvcenter.com/">
    <img src=".github/ovc-logo.svg" alt="Open vCenter" width="512">
  </a>
</h1>

<p align="center">
  <strong>An open source virtualization center to manage hundreds of thousands of VMs</strong>
</p>

<p align="center">
  <a href="https://openvcenter.com">Website</a> ·
  <a href="https://openvcenter.com/architecture">Architecture</a> ·
  <a href="https://openvcenter.com/running">Run it</a> ·
  <a href="https://openvcenter.com/docker-compose">Docker Compose</a> ·
  <a href="https://openvcenter.com/kubernetes">Kubernetes</a> ·
  <a href="https://github.com/claudio-azevedo/Open-Virtualization-Manager/releases">Release Notes</a>
</p>

# ovc-backend

This is a component of the Open vCenter stack.

It is one of three services:

| Service                   | Role                                                                        |
| -------------------------- | --------------------------------------------------------------------------- |
| `ovc-frontend`             | React SSR web UI. Talks to `ovc-backend` over REST.                         |
| `ovc-backend` (this repo) | Python API + worker. Owns Postgres/Valkey, talks to agents over RabbitMQ.    |
| `ovc-agent`                | Go binary on each Hyper-V host. Executes operations, replies over RabbitMQ. |

`ovc-frontend` is the **only** REST client - the contract it expects is
`../ovc-frontend/docs/api-contract.md`. The backend and the agents communicate
**only** through RabbitMQ:

```
ovc-frontend ──REST /api/*──▶ ovc-backend ──RabbitMQ──▶ ovc-agent (per host)
                                  │
                          Postgres + Valkey
```

> A pre-built image is published to GHCR as
> [`ghcr.io/claudio-azevedo/ovc-backend`](https://github.com/claudio-azevedo/Open-vCenter-Backend/pkgs/container/ovc-backend) -
> see [Docker Compose](https://openvcenter.com/docker-compose) or
> [Kubernetes](https://openvcenter.com/kubernetes) to run the whole stack with it.
> Everything from here down is for **local development**, running this app
> straight from source.

## Two processes

| Process    | Command                | Role                                                                                                            |
| ---------- | ---------------------- | --------------------------------------------------------------------------------------------------------------- |
| **API**    | `uvicorn app.main:app` | serves `/api/*`, publishes agent requests, creates tasks; on startup ensures each host's RabbitMQ queues exist  |
| **Worker** | `python -m app.worker` | consumes every host's `response` + inventory queues, updates Postgres + the Valkey cache, times out stale tasks |

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · asyncpg · Alembic · aio-pika ·
redis-py · Python 3.11+.

## Infra first

Postgres, Valkey, RabbitMQ (and Keycloak) live in **`../infra-containers`**, not
here. Start them once:

```sh
cd ../infra-containers && ./start-containers.sh
```

That publishes on localhost: postgres `:5432` (`ovc` / `ovc123` / db `ovc`),
rabbitmq `:5672` (+ UI `:15672`), valkey `:6379`, pgAdmin `:8088`
(`admin@ovc.dev` / `admin`), keycloak `:8080`.

## Run with Docker

`ovc-backend/docker-compose.yml` runs only the **API** and **worker**; they reach the
infra via `host.docker.internal`. The API container runs migrations on start. The
database starts **empty** - register hosts with `POST /api/hosts`.

```sh
docker compose up -d
curl localhost:8000/api/health          # {"ok":true,"db":true,"cache":true,"rabbit":true}
```

Optional stand-in for the Go agent (responds for hosts you've already created,
posts synthetic inventory + executes power actions - no Windows needed):

```sh
docker compose --profile demo up -d
```

## Run locally (without Docker)

Requires Python 3.11+ and the infra from `../infra-containers` up.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip3 install -e ".[dev]"          # or: uv sync

cp .env.example .env             # defaults already match ../infra-containers

alembic upgrade head

uvicorn app.main:app --reload --port 8000    # terminal 1
python -m app.worker                          # terminal 2
python scripts/fake_agent.py                   # terminal 3 (optional demo agent)
```

The DB starts empty - create a host with `POST /api/hosts {name}`, then use
its **Setup Agent** config on the real host (or run `fake_agent.py` for a demo).
The host's FQDN and IP are filled in by the agent on its first `agent_status`.

## Environment

All variables are prefixed `OVC_` and read from `.env` (or the process env).
Docker Compose sets them itself; `.env.example` is the template for local runs.

| Variable                                                     | Default                                              | Purpose                                                                                                   |
| ------------------------------------------------------------ | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `OVC_DATABASE_URL`                                           | `postgresql+asyncpg://ovc:ovc123@localhost:5432/ovc` | async Postgres DSN                                                                                        |
| `OVC_VALKEY_URL`                                             | `redis://localhost:6379/0`                           | Valkey / Redis URL                                                                                        |
| `OVC_RABBITMQ_URL`                                           | `amqp://ovc:ovc123@localhost:5672/`                  | RabbitMQ URL (backend ⇄ broker)                                                                           |
| `OVC_AGENT_RABBITMQ_URL`                                     | - (derives from `OVC_RABBITMQ_URL`)                  | AMQP URL agents use to reach the broker, **without** credentials; shown in the host "Setup Agent" tab     |
| `OVC_API_PREFIX`                                             | `/api`                                               | mount point for all routes                                                                                |
| `OVC_CORS_ORIGINS`                                           | `["http://localhost:3000"]`                          | JSON list of allowed origins                                                                              |
| `OVC_AUTH_MODE`                                              | `stub`                                               | `stub` = trust every request as `OVC_STUB_USER_EMAIL`; `oidc` = verify OIDC JWTs                          |
| `OVC_STUB_USER_EMAIL`                                        | `operator@ovc.local`                                 | the dev principal (global scope grant + `OVC_ADMIN_ROLE`). Set the same `OVC_AUTH_MODE=stub` on the frontend too. |
| `OVC_OIDC_ISSUER` / `_AUDIENCE` / `_JWKS_URL` / `_CLIENT_ID` | -                                                    | required when `OVC_AUTH_MODE=oidc`                                                                        |
| `OVC_OIDC_ROLES_CLAIM`                                       | `resource_access.${client_id}.roles`                 | dot-path to the roles array in the token (`${client_id}` substituted); e.g. `realm_access.roles`, `roles` |
| `OVC_ADMIN_ROLE`                                             | `ADMINISTRATOR`                                      | this role bypasses every scope filter (full access, no `ScopeGrant`)                                      |
| `OVC_AGENT_OFFLINE_AFTER_SECONDS`                            | `120`                                                | host is "offline" if `agent_status` is older than this                                                    |
| `OVC_TASK_TIMEOUT_SECONDS`                                   | `300`                                                | worker marks a task `timeout` after this                                                                  |
| `OVC_AGENT_STORAGE`                                          | `local`                                              | `local` \| `s3` - where agent binaries live (config only for now)                                         |
| `OVC_LOG_LEVEL` / `OVC_LOG_JSON`                             | `INFO` / `false`                                     | logging                                                                                                   |

## API surface

Everything under `OVC_API_PREFIX` (default `/api`). See
`../ovc-frontend/docs/api-contract.md` for the full contract; interactive docs at
`/docs`.

- `GET /health`, `GET /me`
- `GET /clusters`, `/clusters/{id}`
- `GET /hosts?clusterId`, `/hosts/{id}`, `/hosts/{id}/{vms,templates,isos}`
- `GET /hosts/{id}/agent-config` - agent `config.ini` for onboarding (admin only; RabbitMQ creds)
- `GET /folders?hostId&clusterId`
- `GET /vms?hostId&folderId&state`, `/vms/{id}`
- `POST /vms/{id}/actions/{start|stop|shutdown|restart|pause}` → `202 { task }`
- `DELETE /vms/{id}` → `202 { task }`
- `GET /tasks?vmId&hostId&status&limit`, `/tasks/{id}`
- `GET /templates`, `/isos`

Organization (synchronous, DB only - no agent):

- `POST /clusters` `{name}` · `PATCH /clusters/{id}` `{name}` · `DELETE /clusters/{id}` (must be empty - no member hosts)
- `POST /hosts` `{name, clusterId?}` · `PATCH /hosts/{id}` any subset of `{name, fqdn, clusterId}` (`clusterId: null` = standalone; `fqdn` normally left to the agent) · `DELETE /hosts/{id}` (cascades VMs/folders/templates/ISOs)
- `POST /folders` `{name, clusterId? | hostId?}` · `PATCH /folders/{id}` `{name}` · `DELETE /folders/{id}`
- `PATCH /vms/{id}` `{folderId}` - move a VM into a folder or out (`null`)

Responses are **camelCase** JSON. Errors: `{ "error": { "code", "message", "details"? } }`.

## RabbitMQ queues

Per host id (`<hostid>` = random 10-char `[A-Za-z0-9]`):

| Queue                                                    | Direction       | Notes                                           |
| -------------------------------------------------------- | --------------- | ----------------------------------------------- |
| `<hostid>.request`                                       | backend → agent | JSON `AgentRequest`, `correlation_id = task id` |
| `<hostid>.response`                                      | agent → backend | task progress + result                          |
| `<hostid>.agent_status`                                  | agent → backend | last value only                                 |
| `<hostid>.vm_inventory`                                  | agent → backend | last value only                                 |
| `<hostid>.host_inventory`                                | agent → backend | last value only (hardware + folders)            |
| `<hostid>.template_inventory` / `<hostid>.iso_inventory` | agent → backend | last value only                                 |

To lock a per-host agent user to its own queues, set its RabbitMQ permissions to the
regex from `app.messaging.agent_permission_pattern(host_id)`.

## Migrations

```sh
alembic upgrade head
alembic revision --autogenerate -m "describe change"
```

`alembic/env.py` takes the DSN from `app.config`, so `OVC_DATABASE_URL` must be set.

## Project layout

See [`LLM.md`](LLM.md) for an annotated map of `app/` and the async task lifecycle.

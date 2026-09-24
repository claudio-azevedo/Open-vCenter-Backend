from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ...cache import get_vm_locks, get_vm_states
from ...config import get_settings
from ...models import Host, HostMetric, Iso, Template, Vm
from ...schemas import (
    HostAgentConfigOut,
    HostAgentInstallOut,
    HostCreate,
    HostDetailOut,
    HostMetricSample,
    HostOut,
    HostUpdate,
    IsoOut,
    TaskEnvelope,
    TemplateOut,
    VmOut,
)
from ...schemas.agent_binary import AgentUpgradeRequest
from ...services.agent_store import verify_scope
from ...services.agent_upgrade import request_agent_upgrade, resolve_upgrade_binary
from ...services.host_actions import DISRUPTIVE_HOST_ACTIONS, request_host_action
from ...services.hosts import (
    agent_amqp_url,
    build_agent_install_script,
    build_agent_install_url,
    create_host,
    install_script_filename,
    provision_host_agent_user,
    render_agent_config,
)
from ...services.organization import delete_host, move_host
from ...services.serializers import (
    agent_connected,
    host_detail_out,
    host_metric_out,
    host_out,
    iso_out,
    task_out,
    template_out,
    vm_out,
)
from ..deps import CurrentUser, DbSession, Scope
from ..errors import ApiError, forbidden, not_found

router = APIRouter(tags=["hosts"])


async def _vm_counts(db: DbSession) -> dict[str, int]:
    rows = (
        await db.execute(
            select(Vm.host_id, func.count()).group_by(Vm.host_id)
        )
    ).all()
    return dict(rows)


@router.get("/hosts", response_model=list[HostOut])
async def list_hosts(
    db: DbSession,
    scope: Scope,
    cluster_id: str | None = Query(default=None, alias="clusterId"),
) -> list[HostOut]:
    stmt = select(Host).order_by(Host.name)
    if cluster_id is not None:
        stmt = stmt.where(Host.cluster_id == cluster_id)
    hosts = (await db.execute(stmt)).scalars().all()
    counts = await _vm_counts(db)
    return [
        host_out(h, counts.get(h.id, 0)) for h in hosts if scope.sees_host(h.id)
    ]


@router.post("/hosts", response_model=HostDetailOut, status_code=status.HTTP_201_CREATED)
async def add_host(body: HostCreate, db: DbSession, scope: Scope) -> HostDetailOut:
    if not scope.all and not (
        body.cluster_id and scope.sees_cluster(body.cluster_id)
    ):
        raise forbidden("You cannot add a host here")
    host = await create_host(
        db,
        name=body.name,
        cluster_id=body.cluster_id,
        hypervisor=body.hypervisor,
    )
    return host_detail_out(host, 0)


async def _get_visible_host(db: DbSession, scope: Scope, host_id: str) -> Host:
    host = await db.get(Host, host_id)
    if host is None:
        raise not_found("Host")
    if not scope.sees_host(host.id):
        raise forbidden()
    return host


@router.get("/hosts/{host_id}", response_model=HostDetailOut)
async def get_host(host_id: str, db: DbSession, scope: Scope) -> HostDetailOut:
    host = await _get_visible_host(db, scope, host_id)
    count = (await _vm_counts(db)).get(host.id, 0)
    return host_detail_out(host, count)


@router.patch("/hosts/{host_id}", response_model=HostDetailOut)
async def update_host(
    host_id: str, body: HostUpdate, db: DbSession, scope: Scope
) -> HostDetailOut:
    host = await _get_visible_host(db, scope, host_id)
    fields = body.model_fields_set

    if "cluster_id" in fields:
        if not scope.all and not (
            body.cluster_id and scope.sees_cluster(body.cluster_id)
        ):
            raise forbidden("You cannot move this host")
        host = await move_host(db, host, body.cluster_id)

    if "name" in fields and body.name is not None:
        host.name = body.name.strip()
    if "fqdn" in fields and body.fqdn is not None:
        host.fqdn = body.fqdn.strip()
    await db.flush()

    count = (await _vm_counts(db)).get(host.id, 0)
    return host_detail_out(host, count)


@router.delete("/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_host(host_id: str, db: DbSession, scope: Scope) -> None:
    host = await _get_visible_host(db, scope, host_id)
    if not scope.all:
        raise forbidden("Only an administrator can remove hosts")
    await delete_host(db, host)


@router.get("/hosts/{host_id}/agent-config", response_model=HostAgentConfigOut)
async def get_host_agent_config(
    host_id: str, db: DbSession, scope: Scope
) -> HostAgentConfigOut:
    """The agent `config.ini` for onboarding this host (id + RabbitMQ URL with
    per-host credentials). Admin-only - it exposes RabbitMQ credentials."""
    host = await _get_visible_host(db, scope, host_id)
    if not scope.all:
        raise forbidden("Only an administrator can view agent credentials")
    # (re)assert the RabbitMQ user + permissions every time this is opened - cheap,
    # idempotent, and self-heals a host whose agent still can't connect.
    await provision_host_agent_user(db, host)
    return HostAgentConfigOut(
        host_id=host.short_id,
        rabbitmq_url=agent_amqp_url(host),
        config_ini=render_agent_config(host),
    )


@router.get("/hosts/{host_id}/agent-install-url", response_model=HostAgentInstallOut)
async def get_host_agent_install_url(
    host_id: str, db: DbSession, scope: Scope
) -> HostAgentInstallOut:
    """Mint a tokenized `agent-install.ps1` URL for this host plus an elevated-
    PowerShell one-liner that fetches and runs it - the "copy install command"
    button on the Setup Agent tab. Admin-only; the URL's HMAC token then stands
    in for a login until it expires (`OVC_AGENT_INSTALL_URL_TTL_SECONDS`)."""
    host = await _get_visible_host(db, scope, host_id)
    if not scope.all:
        raise forbidden("Only an administrator can issue an agent installer URL")
    await provision_host_agent_user(db, host)
    url, command, expires_at = await build_agent_install_url(db, host)
    return HostAgentInstallOut(
        url=url,
        command=command,
        expires_at=expires_at,
        filename=install_script_filename(host),
    )


@router.get("/hosts/{host_id}/agent-install.ps1")
async def get_host_agent_install_script(host_id: str, request: Request, db: DbSession):
    """A self-contained `install.ps1` for this host: it creates the install dir,
    downloads + checksums the active agent binary, writes `config.ini`, installs
    the Windows service, opens the config in Notepad, and prints how to start the
    service. Authenticated by the short-lived HMAC token in the query string
    (minted by `GET .../agent-install-url`) - a Hyper-V host running the
    PowerShell one-liner can't send a session cookie."""
    from fastapi import Response

    host = await db.get(Host, host_id)
    if host is None:
        raise not_found("Host")
    if not verify_scope(
        f"host-install:{host_id}",
        request.query_params.get("exp"),
        request.query_params.get("token"),
    ):
        raise ApiError(
            "FORBIDDEN", "Invalid or expired install token", status_code=403
        )
    await provision_host_agent_user(db, host)
    script, filename = await build_agent_install_script(db, host)
    return Response(
        content=script,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/hosts/{host_id}/actions/update-agent",
    response_model=TaskEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def update_host_agent(
    host_id: str,
    db: DbSession,
    scope: Scope,
    user: CurrentUser,
    body: AgentUpgradeRequest | None = None,
) -> TaskEnvelope:
    """Queue a `host_update_agent` task - the agent downloads the target build,
    verifies it, and swaps itself in drain mode. Admin-only."""
    host = await _get_visible_host(db, scope, host_id)
    if not scope.all:
        raise forbidden("Only an administrator can upgrade the agent")
    binary = await resolve_upgrade_binary(db, host, body.binary_id if body else None)
    task = await request_agent_upgrade(db, host, binary=binary, requested_by=user.email)
    return TaskEnvelope(task=task_out(task))


@router.post(
    "/hosts/{host_id}/actions/{action}",
    response_model=TaskEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def host_action(
    host_id: str,
    action: str,
    db: DbSession,
    scope: Scope,
    user: CurrentUser,
) -> TaskEnvelope:
    """Queue a host operation on its agent: Failover Cluster node maintenance
    (`suspend`, `suspend_drain`, `resume`, `resume_fallback`), a host `restart`,
    or a forced `refresh_hardware` / `refresh_inventory`. Node maintenance and
    restart are admin-only."""
    host = await _get_visible_host(db, scope, host_id)
    if action in DISRUPTIVE_HOST_ACTIONS and not scope.all:
        raise forbidden("Only an administrator can change a host's cluster or power state")
    task = await request_host_action(db, host, action, requested_by=user.email)
    return TaskEnvelope(task=task_out(task))


@router.get("/hosts/{host_id}/metrics", response_model=list[HostMetricSample])
async def list_host_metrics(
    host_id: str, db: DbSession, scope: Scope
) -> list[HostMetricSample]:
    """Quick host metrics for the last hour (CPU, memory, disk latency, network),
    oldest sample first. Not a monitoring stack - see app/models/metric.py."""
    await _get_visible_host(db, scope, host_id)
    since = datetime.now(UTC) - timedelta(
        seconds=get_settings().metrics_retention_seconds
    )
    rows = (
        await db.execute(
            select(HostMetric)
            .where(HostMetric.host_id == host_id)
            .where(HostMetric.ts >= since)
            .order_by(HostMetric.ts)
        )
    ).scalars().all()
    return [host_metric_out(r) for r in rows]


@router.get("/hosts/{host_id}/vms", response_model=list[VmOut])
async def list_host_vms(host_id: str, db: DbSession, scope: Scope) -> list[VmOut]:
    host = await _get_visible_host(db, scope, host_id)
    host_offline = not agent_connected(host)
    vms = (
        await db.execute(
            select(Vm)
            .where(Vm.host_id == host_id)
            .options(selectinload(Vm.disks), selectinload(Vm.nics))
            .order_by(Vm.name)
        )
    ).scalars().all()
    ids = [v.id for v in vms]
    states = await get_vm_states(ids)
    locks = await get_vm_locks(ids)
    return [
        vm_out(v, states.get(v.id), locks.get(v.id), host_offline=host_offline)
        for v in vms
    ]


@router.get("/hosts/{host_id}/templates", response_model=list[TemplateOut])
async def list_host_templates(
    host_id: str, db: DbSession, scope: Scope
) -> list[TemplateOut]:
    await _get_visible_host(db, scope, host_id)
    rows = (
        await db.execute(
            select(Template).where(Template.host_id == host_id).order_by(Template.name)
        )
    ).scalars().all()
    return [template_out(t) for t in rows]


@router.get("/hosts/{host_id}/isos", response_model=list[IsoOut])
async def list_host_isos(host_id: str, db: DbSession, scope: Scope) -> list[IsoOut]:
    await _get_visible_host(db, scope, host_id)
    rows = (
        await db.execute(
            select(Iso).where(Iso.host_id == host_id).order_by(Iso.name)
        )
    ).scalars().all()
    return [iso_out(i) for i in rows]

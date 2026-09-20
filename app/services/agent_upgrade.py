"""Queue a ``host_update_agent`` task - the agent self-upgrade.

The Hyper-V agent already implements the wire contract
(``ovc-agent-hyperv/internal/tasks/agent_upgrade.go``): it downloads the binary
from ``download_url``, verifies ``checksum_sha256``, stages
``ovc-agent-<version>.exe``, enters drain mode, swaps the binary and restarts,
and the **new** agent publishes the terminal ``succeeded`` response. It rejects
an upgrade whose ``version`` equals its running version, so we pre-check that
here for a clean 409.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.errors import ApiError
from ..ids import new_id
from ..messaging import AgentRequest, publish_request
from ..models import AgentBinary, Host, Task
from .agent_store import get_agent_store
from .serializers import agent_connected

log = logging.getLogger(__name__)

_NON_TERMINAL = ("queued", "running")


async def active_agent_binary(db: AsyncSession, hypervisor: str) -> AgentBinary | None:
    return (
        await db.execute(
            select(AgentBinary)
            .where(AgentBinary.hypervisor == hypervisor)
            .where(AgentBinary.is_active.is_(True))
        )
    ).scalar_one_or_none()


async def resolve_upgrade_binary(
    db: AsyncSession, host: Host, binary_id: str | None
) -> AgentBinary:
    if binary_id is not None:
        binary = await db.get(AgentBinary, binary_id)
        if binary is None:
            raise ApiError("NOT_FOUND", "Agent binary not found", status_code=404)
    else:
        binary = await active_agent_binary(db, host.hypervisor)
        if binary is None:
            raise ApiError(
                "NO_ACTIVE_AGENT_BINARY",
                f"No active agent binary for hypervisor '{host.hypervisor}' - "
                "upload one in Agent Management first",
                status_code=409,
            )
    if binary.hypervisor != host.hypervisor:
        raise ApiError(
            "INVALID",
            f"Agent binary is for '{binary.hypervisor}', host runs '{host.hypervisor}'",
        )
    return binary


async def request_agent_upgrade(
    db: AsyncSession, host: Host, *, binary: AgentBinary, requested_by: str
) -> Task:
    if not agent_connected(host):
        raise ApiError(
            "HOST_OFFLINE",
            f"Host '{host.name}' agent is offline - it can't receive an upgrade "
            "until it reconnects",
            status_code=409,
        )
    if binary.version == host.agent_version:
        raise ApiError(
            "AGENT_ALREADY_CURRENT",
            f"Host '{host.name}' already runs agent {binary.version}",
            status_code=409,
        )

    existing = (
        await db.execute(
            select(Task)
            .where(Task.host_id == host.id)
            .where(Task.kind == "host_update_agent")
            .where(Task.status.in_(_NON_TERMINAL))
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiError(
            "AGENT_UPGRADE_IN_PROGRESS",
            f"An agent upgrade is already running on '{host.name}'",
            status_code=409,
            details={"taskId": existing.id},
        )

    store = get_agent_store()
    url = await store.download_url(binary.id, binary.storage_key, binary.filename)
    if not url:
        raise ApiError(
            "AGENT_DOWNLOAD_UNAVAILABLE",
            "Local agent storage has no public download URL - set "
            "OVC_PUBLIC_BASE_URL so hosts can reach this API",
            status_code=409,
        )

    task = Task(
        id=new_id(),
        kind="host_update_agent",
        status="queued",
        target_type="host",
        target_id=host.id,
        target_name=host.name,
        host_id=host.id,
        requested_by=requested_by,
        progress=0,
    )
    task.correlation_id = task.id
    db.add(task)

    request = AgentRequest(
        id=task.id,
        function="host_update_agent",
        params={
            "download_url": url,
            "checksum_sha256": binary.checksum_sha256,
            "version": binary.version,
        },
        requested_by=requested_by,
    )
    task.request_payload = request.model_dump(mode="json")
    await db.flush()

    await publish_request(host.short_id, request)
    log.info(
        "queued agent upgrade host=%s -> %s (task=%s)",
        host.short_id,
        binary.version,
        task.id,
    )
    return task

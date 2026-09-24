"""Queue host-level operations on a host's agent: Failover Cluster node
maintenance (pause / drain / resume / failback), a host reboot, and forced
hardware / VM inventory refreshes.

The Hyper-V agent handles all of them in ``host_management.go``; the wire
``function`` is the action name itself (the agent maps it onto its
host_management handler). Pause/resume report the node's new cluster state
(``Paused`` / ``Up``) in ``result.status`` - ``apply_host_action_response``
folds it into the host's hardware snapshot so the UI flips immediately instead
of waiting for the next hardware inventory.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.errors import ApiError
from ..ids import new_id
from ..messaging import AgentRequest, AgentResponse, publish_request
from ..models import Host, Task
from .serializers import agent_connected

log = logging.getLogger(__name__)

_NON_TERMINAL = ("queued", "running")

# Failover Cluster node maintenance - only valid on a clustered host.
CLUSTER_NODE_ACTIONS = frozenset(
    {"suspend", "suspend_drain", "resume", "resume_fallback"}
)
# Actions that change the host / node state - admin-only, one at a time.
DISRUPTIVE_HOST_ACTIONS = CLUSTER_NODE_ACTIONS | {"restart"}
HOST_ACTIONS = DISRUPTIVE_HOST_ACTIONS | {"refresh_hardware", "refresh_inventory"}


def host_is_clustered(host: Host) -> bool:
    """In an app cluster, or a Windows Failover Cluster member per its report."""
    reported = ((host.hardware or {}).get("cluster") or {}).get("clustered")
    return host.cluster_id is not None or bool(reported)


async def request_host_action(
    db: AsyncSession, host: Host, action: str, *, requested_by: str
) -> Task:
    if action not in HOST_ACTIONS:
        raise ApiError("UNKNOWN_ACTION", f"Unknown host action '{action}'", status_code=404)
    if not agent_connected(host):
        raise ApiError(
            "HOST_OFFLINE",
            f"Host '{host.name}' agent is offline - host actions are unavailable "
            "until it reconnects",
            status_code=409,
        )
    if action in CLUSTER_NODE_ACTIONS and not host_is_clustered(host):
        raise ApiError(
            "NOT_CLUSTERED",
            f"Host '{host.name}' is not a Failover Cluster node",
            status_code=409,
        )
    if action in DISRUPTIVE_HOST_ACTIONS:
        # never stack a drain on a restart (or vice versa) on the same node
        existing = (
            await db.execute(
                select(Task)
                .where(Task.host_id == host.id)
                .where(Task.target_type == "host")
                .where(Task.kind.in_(DISRUPTIVE_HOST_ACTIONS))
                .where(Task.status.in_(_NON_TERMINAL))
            )
        ).scalars().first()
        if existing is not None:
            raise ApiError(
                "HOST_ACTION_IN_PROGRESS",
                f"A '{existing.kind}' operation is already running on '{host.name}'",
                status_code=409,
                details={"taskId": existing.id},
            )

    task = Task(
        id=new_id(),
        kind=action,
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
        id=task.id, function=action, params={}, requested_by=requested_by
    )
    task.request_payload = request.model_dump(mode="json")
    await db.flush()

    await publish_request(host.short_id, request)
    log.info("queued host action %s host=%s (task=%s)", action, host.short_id, task.id)
    return task


async def apply_host_action_response(
    db: AsyncSession, task: Task, resp: AgentResponse
) -> None:
    """After a successful pause/resume, store the node state the agent reported
    (``result.status``, e.g. ``Paused`` / ``Up``) on the host's hardware
    snapshot."""
    if task.kind not in CLUSTER_NODE_ACTIONS or task.status != "succeeded":
        return
    state = resp.result.get("status") if isinstance(resp.result, dict) else None
    if not state:
        return
    host = await db.get(Host, task.target_id)
    if host is None or not host.hardware:
        return
    hardware = dict(host.hardware)
    cluster = dict(hardware.get("cluster") or {})
    cluster["state"] = str(state)
    hardware["cluster"] = cluster
    # reassign so SQLAlchemy sees the JSONB change
    host.hardware = hardware

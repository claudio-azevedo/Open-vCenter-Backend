from __future__ import annotations

import logging

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.errors import ApiError, not_found
from ..ids import new_id
from ..models import Cluster, Host, Vlan

log = logging.getLogger(__name__)


async def list_vlans(
    db: AsyncSession,
    *,
    host_id: str | None = None,
    cluster_id: str | None = None,
) -> list[Vlan]:
    """VLANs visible from a scope.

    ``hostId`` resolves to the host's own VLANs plus, when the host belongs to a
    cluster, that cluster's VLANs (agents apply the tag regardless of where the
    definition lives). ``clusterId`` returns just that cluster's VLANs.
    """
    stmt = select(Vlan).order_by(Vlan.vlan_id)
    if host_id is not None:
        host = await db.get(Host, host_id)
        if host is None:
            raise not_found("Host")
        if host.cluster_id is not None:
            stmt = stmt.where(
                or_(Vlan.host_id == host_id, Vlan.cluster_id == host.cluster_id)
            )
        else:
            stmt = stmt.where(Vlan.host_id == host_id)
    elif cluster_id is not None:
        stmt = stmt.where(Vlan.cluster_id == cluster_id)
    return list((await db.execute(stmt)).scalars().all())


async def _clear_default(db: AsyncSession, *, cluster_id: str | None, host_id: str | None) -> None:
    where = Vlan.cluster_id == cluster_id if cluster_id else Vlan.host_id == host_id
    await db.execute(update(Vlan).where(where).values(is_default=False))


async def create_vlan(
    db: AsyncSession,
    *,
    name: str,
    vlan_id: int,
    description: str | None,
    is_default: bool,
    cluster_id: str | None,
    host_id: str | None,
) -> Vlan:
    if bool(cluster_id) == bool(host_id):
        raise ApiError("INVALID", "Provide exactly one of clusterId or hostId")

    if cluster_id is not None:
        if await db.get(Cluster, cluster_id) is None:
            raise not_found("Cluster")
    else:
        host = await db.get(Host, host_id)
        if host is None:
            raise not_found("Host")
        if host.cluster_id is not None:
            raise ApiError(
                "INVALID",
                "This host is in a cluster - create the VLAN on the cluster instead",
            )

    existing = await db.scalar(
        select(Vlan).where(
            Vlan.vlan_id == vlan_id,
            Vlan.cluster_id == cluster_id if cluster_id else Vlan.host_id == host_id,
        )
    )
    if existing is not None:
        raise ApiError("INVALID", f"VLAN {vlan_id} is already defined in this scope")

    if is_default:
        await _clear_default(db, cluster_id=cluster_id, host_id=host_id)

    vlan = Vlan(
        id=new_id(),
        name=name.strip(),
        vlan_id=vlan_id,
        description=(description or "").strip() or None,
        is_default=is_default,
        cluster_id=cluster_id,
        host_id=host_id,
    )
    db.add(vlan)
    await db.flush()
    log.info("created vlan=%s tag=%s", vlan.id, vlan.vlan_id)
    return vlan


async def update_vlan(
    db: AsyncSession,
    vlan: Vlan,
    *,
    name: str | None = None,
    description: str | None = None,
    is_default: bool | None = None,
) -> Vlan:
    if name is not None:
        vlan.name = name.strip()
    if description is not None:
        vlan.description = description.strip() or None
    if is_default is not None:
        if is_default:
            await _clear_default(
                db, cluster_id=vlan.cluster_id, host_id=vlan.host_id
            )
        vlan.is_default = is_default
    await db.flush()
    return vlan


async def delete_vlan(db: AsyncSession, vlan: Vlan) -> None:
    await db.delete(vlan)
    await db.flush()


async def resolve_host_vlan(db: AsyncSession, host: Host, tag: int) -> Vlan:
    """The VLAN with numeric ``tag`` reachable from ``host`` (own or cluster)."""
    stmt = select(Vlan).where(Vlan.vlan_id == tag)
    if host.cluster_id is not None:
        stmt = stmt.where(
            or_(Vlan.host_id == host.id, Vlan.cluster_id == host.cluster_id)
        )
    else:
        stmt = stmt.where(Vlan.host_id == host.id)
    vlan = await db.scalar(stmt)
    if vlan is None:
        raise ApiError("INVALID", f"VLAN {tag} is not defined for this host")
    return vlan

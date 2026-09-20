from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Folder, Host, ScopeGrant, User, Vm


@dataclass
class VisibleScope:
    """What a principal is allowed to see. `all=True` short-circuits every filter."""

    all: bool = False
    cluster_ids: set[str] = field(default_factory=set)
    host_ids: set[str] = field(default_factory=set)
    folder_ids: set[str] = field(default_factory=set)

    def sees_cluster(self, cluster_id: str | None) -> bool:
        return self.all or (cluster_id is not None and cluster_id in self.cluster_ids)

    def sees_host(self, host_id: str) -> bool:
        return self.all or host_id in self.host_ids

    def sees_folder(self, folder_id: str | None) -> bool:
        return self.all or (folder_id is not None and folder_id in self.folder_ids)


async def resolve_scope(db: AsyncSession, user: User) -> VisibleScope:
    # The admin role (from the IdP token) sees everything, no ScopeGrant required.
    admin_role = get_settings().admin_role
    user_roles = user.roles.split(",") if user.roles else []
    if admin_role in user_roles:
        return VisibleScope(all=True)

    grants = (
        await db.execute(select(ScopeGrant).where(ScopeGrant.user_id == user.id))
    ).scalars().all()

    scope = VisibleScope()
    cluster_grants: set[str] = set()
    host_grants: set[str] = set()
    folder_grants: set[str] = set()

    for g in grants:
        if g.level == "global":
            scope.all = True
            return scope
        if g.level == "cluster" and g.scope_id:
            cluster_grants.add(g.scope_id)
        elif g.level == "host" and g.scope_id:
            host_grants.add(g.scope_id)
        elif g.level == "folder" and g.scope_id:
            folder_grants.add(g.scope_id)

    scope.cluster_ids = cluster_grants

    # hosts in granted clusters + explicitly granted hosts
    host_ids = set(host_grants)
    if cluster_grants:
        rows = (
            await db.execute(
                select(Host.id).where(Host.cluster_id.in_(cluster_grants))
            )
        ).scalars().all()
        host_ids.update(rows)
    scope.host_ids = host_ids

    # folders on visible hosts + explicitly granted folders
    folder_ids = set(folder_grants)
    if host_ids:
        rows = (
            await db.execute(
                select(Folder.id).where(Folder.host_id.in_(host_ids))
            )
        ).scalars().all()
        folder_ids.update(rows)
    scope.folder_ids = folder_ids

    return scope


def vm_visible(scope: VisibleScope, vm: Vm) -> bool:
    return (
        scope.all
        or scope.sees_host(vm.host_id)
        or scope.sees_folder(vm.folder_id)
    )

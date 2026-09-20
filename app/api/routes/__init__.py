from __future__ import annotations

from fastapi import APIRouter

from . import (
    agent_binaries,
    auth,
    clusters,
    folders,
    health,
    hosts,
    images,
    tasks,
    vlans,
    vms,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(clusters.router)
api_router.include_router(hosts.router)
api_router.include_router(agent_binaries.router)
api_router.include_router(folders.router)
api_router.include_router(vlans.router)
api_router.include_router(vms.router)
api_router.include_router(tasks.router)
api_router.include_router(images.router)

__all__ = ["api_router"]

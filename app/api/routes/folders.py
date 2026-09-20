from __future__ import annotations

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from ...models import Folder
from ...schemas import FolderCreate, FolderOut, FolderUpdate
from ...services.organization import (
    create_folder,
    delete_folder,
    rename_folder,
)
from ...services.serializers import folder_out
from ..deps import DbSession, Scope
from ..errors import forbidden, not_found

router = APIRouter(tags=["folders"])


def _visible(scope: Scope, folder: Folder) -> bool:
    return (
        scope.all
        or (folder.cluster_id is not None and scope.sees_cluster(folder.cluster_id))
        or (folder.host_id is not None and scope.sees_host(folder.host_id))
    )


@router.get("/folders", response_model=list[FolderOut])
async def list_folders(
    db: DbSession,
    scope: Scope,
    host_id: str | None = Query(default=None, alias="hostId"),
    cluster_id: str | None = Query(default=None, alias="clusterId"),
) -> list[FolderOut]:
    stmt = select(Folder).order_by(Folder.name)
    if host_id is not None:
        stmt = stmt.where(Folder.host_id == host_id)
    if cluster_id is not None:
        stmt = stmt.where(Folder.cluster_id == cluster_id)
    folders = (await db.execute(stmt)).scalars().all()
    return [folder_out(f) for f in folders if _visible(scope, f)]


@router.post("/folders", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
async def add_folder(body: FolderCreate, db: DbSession, scope: Scope) -> FolderOut:
    allowed = scope.all
    if body.cluster_id:
        allowed = allowed or scope.sees_cluster(body.cluster_id)
    if body.host_id:
        allowed = allowed or scope.sees_host(body.host_id)
    if not allowed:
        raise forbidden("You cannot create a folder here")
    folder = await create_folder(
        db, name=body.name, cluster_id=body.cluster_id, host_id=body.host_id
    )
    return folder_out(folder)


async def _get_visible_folder(db: DbSession, scope: Scope, folder_id: str) -> Folder:
    folder = await db.get(Folder, folder_id)
    if folder is None:
        raise not_found("Folder")
    if not _visible(scope, folder):
        raise forbidden()
    return folder


@router.patch("/folders/{folder_id}", response_model=FolderOut)
async def patch_folder(
    folder_id: str, body: FolderUpdate, db: DbSession, scope: Scope
) -> FolderOut:
    folder = await _get_visible_folder(db, scope, folder_id)
    folder = await rename_folder(db, folder, body.name)
    return folder_out(folder)


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_folder(folder_id: str, db: DbSession, scope: Scope) -> Response:
    folder = await _get_visible_folder(db, scope, folder_id)
    await delete_folder(db, folder)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

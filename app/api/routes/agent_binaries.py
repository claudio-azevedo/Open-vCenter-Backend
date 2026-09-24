from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request, UploadFile, status
from sqlalchemy import select

from ...config import get_settings
from ...models import DEFAULT_HYPERVISOR, AgentBinary, Host
from ...schemas import (
    AgentBinaryOut,
    AgentBinaryUpdate,
    AgentRolloutRequest,
    AgentRolloutResult,
    AgentRolloutSkip,
    AgentStorageInfoOut,
)
from ...schemas.task import TaskOut
from ...services.agent_store import (
    AgentStoreError,
    get_agent_store,
    object_key,
    verify_download,
)
from ...services.agent_upgrade import request_agent_upgrade
from ...services.serializers import agent_connected, task_out
from ..deps import CurrentUser, DbSession, Scope
from ..errors import ApiError, forbidden, not_found

log = logging.getLogger(__name__)

router = APIRouter(tags=["agent-binaries"])

_MAX_UPLOAD_BYTES = 128 * 1024 * 1024
_CHUNK = 1 << 20


def _require_admin(scope: Scope) -> None:
    if not scope.all:
        raise forbidden("Only an administrator can manage agent binaries")


def _out(b: AgentBinary) -> AgentBinaryOut:
    return AgentBinaryOut.model_validate(b)


async def _set_active(db: DbSession, binary: AgentBinary) -> None:
    """Make ``binary`` the active build for its hypervisor, demoting the current
    one. Done as two writes with a flush between so the partial unique index
    (one active row per hypervisor) never sees two."""
    current = (
        await db.execute(
            select(AgentBinary)
            .where(AgentBinary.hypervisor == binary.hypervisor)
            .where(AgentBinary.is_active.is_(True))
            .where(AgentBinary.id != binary.id)
        )
    ).scalars().all()
    for row in current:
        row.is_active = False
    if current:
        await db.flush()
    binary.is_active = True


@router.get("/agent-binaries", response_model=list[AgentBinaryOut])
async def list_agent_binaries(
    db: DbSession,
    scope: Scope,
    hypervisor: str | None = Query(default=None),
) -> list[AgentBinaryOut]:
    _require_admin(scope)
    stmt = select(AgentBinary).order_by(AgentBinary.created_at.desc())
    if hypervisor:
        stmt = stmt.where(AgentBinary.hypervisor == hypervisor)
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(b) for b in rows]


@router.get("/agent-binaries/storage", response_model=AgentStorageInfoOut)
async def get_agent_storage(scope: Scope) -> AgentStorageInfoOut:
    _require_admin(scope)
    settings = get_settings()
    store = get_agent_store()
    downloads_enabled = (
        store.backend == "s3" or bool(settings.public_base_url)
    )
    return AgentStorageInfoOut(
        backend=store.backend,
        location=store.describe(),
        download_url_ttl_seconds=settings.agent_download_url_ttl_seconds,
        downloads_enabled=downloads_enabled,
    )


@router.post(
    "/agent-binaries",
    response_model=AgentBinaryOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_agent_binary(
    db: DbSession,
    scope: Scope,
    user: CurrentUser,
    file: UploadFile,
    version: str = Form(...),
    hypervisor: str = Form(default=DEFAULT_HYPERVISOR.value),
    notes: str | None = Form(default=None),
    make_active: bool = Form(default=False, alias="makeActive"),
) -> AgentBinaryOut:
    _require_admin(scope)
    version = version.strip()
    if not version:
        raise ApiError("INVALID", "version is required")
    if not (file.filename or "").lower().endswith(".exe"):
        raise ApiError("INVALID", "agent binary must be an .exe file")

    dup = (
        await db.execute(
            select(AgentBinary)
            .where(AgentBinary.version == version)
            .where(AgentBinary.hypervisor == hypervisor)
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise ApiError(
            "DUPLICATE",
            f"Agent binary {version} ({hypervisor}) already exists",
            status_code=409,
        )

    # stream to a temp file, hashing + counting as we go
    hasher = hashlib.sha256()
    size = 0
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        while chunk := await file.read(_CHUNK):
            if size == 0 and not chunk.startswith(b"MZ"):
                raise ApiError(
                    "INVALID", "uploaded file is not a Windows executable"
                )
            size += len(chunk)
            if size > _MAX_UPLOAD_BYTES:
                raise ApiError(
                    "TOO_LARGE",
                    f"Agent binary exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit",
                    status_code=413,
                )
            hasher.update(chunk)
            tmp.write(chunk)
        tmp.close()
        if size == 0:
            raise ApiError("INVALID", "uploaded file is empty")

        binary = AgentBinary(
            version=version,
            hypervisor=hypervisor,
            filename=Path(file.filename or "ovc-agent.exe").name,
            size_bytes=size,
            checksum_sha256=hasher.hexdigest(),
            content_type=file.content_type or "application/octet-stream",
            storage_backend=get_agent_store().backend,
            storage_key="",  # set below once we have the id
            notes=(notes or None),
            uploaded_by=user.email,
        )
        db.add(binary)
        await db.flush()  # assigns binary.id
        binary.storage_key = object_key(binary.id, binary.filename)

        try:
            await get_agent_store().put_file(
                binary.storage_key, tmp.name, content_type=binary.content_type
            )
        except AgentStoreError as exc:
            raise ApiError("STORAGE_ERROR", str(exc), status_code=502) from exc

        if make_active:
            await _set_active(db, binary)
        await db.flush()
        return _out(binary)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp.name)


@router.patch("/agent-binaries/{binary_id}", response_model=AgentBinaryOut)
async def update_agent_binary(
    binary_id: str, body: AgentBinaryUpdate, db: DbSession, scope: Scope
) -> AgentBinaryOut:
    _require_admin(scope)
    binary = await db.get(AgentBinary, binary_id)
    if binary is None:
        raise not_found("Agent binary")
    fields = body.model_fields_set
    if "notes" in fields:
        binary.notes = body.notes or None
    if "is_active" in fields and body.is_active is not None:
        if body.is_active:
            await _set_active(db, binary)
        else:
            binary.is_active = False
    await db.flush()
    return _out(binary)


@router.delete("/agent-binaries/{binary_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_binary(binary_id: str, db: DbSession, scope: Scope) -> None:
    _require_admin(scope)
    binary = await db.get(AgentBinary, binary_id)
    if binary is None:
        raise not_found("Agent binary")
    if binary.is_active:
        raise ApiError(
            "ACTIVE_BINARY",
            "Promote another build to active before deleting this one",
            status_code=409,
        )
    try:
        await get_agent_store().delete(binary.storage_key)
    except AgentStoreError:
        log.warning("could not delete stored bytes for agent binary %s", binary_id)
    await db.delete(binary)


@router.get("/agent-binaries/{binary_id}/download")
async def download_agent_binary(binary_id: str, request: Request, db: DbSession):
    """Serve the raw binary to a Hyper-V host agent (local storage mode only).
    Guarded by the short-lived HMAC token minted into ``download_url`` - the
    agent can't send an Authorization header. In s3 mode the agent gets a
    presigned URL and never hits this route."""
    from fastapi.responses import StreamingResponse

    binary = await db.get(AgentBinary, binary_id)
    if binary is None:
        raise not_found("Agent binary")
    store = get_agent_store()
    if store.backend != "local":
        raise not_found("Agent binary")
    if not verify_download(
        binary_id,
        request.query_params.get("exp"),
        request.query_params.get("token"),
    ):
        raise ApiError("FORBIDDEN", "Invalid or expired download token", status_code=403)

    return StreamingResponse(
        store.open_stream(binary.storage_key),
        media_type=binary.content_type or "application/octet-stream",
        headers={
            "Content-Length": str(binary.size_bytes),
            "Content-Disposition": f'attachment; filename="{binary.filename}"',
        },
    )


@router.post("/agent-binaries/{binary_id}/rollout", response_model=AgentRolloutResult)
async def rollout_agent_binary(
    binary_id: str,
    body: AgentRolloutRequest,
    db: DbSession,
    scope: Scope,
    user: CurrentUser,
) -> AgentRolloutResult:
    """Queue a ``host_update_agent`` task for each targeted host. Default target:
    every online host on the binary's hypervisor whose agent version differs."""
    _require_admin(scope)
    binary = await db.get(AgentBinary, binary_id)
    if binary is None:
        raise not_found("Agent binary")

    stmt = select(Host).where(Host.hypervisor == binary.hypervisor).order_by(Host.name)
    if body.host_ids:
        stmt = stmt.where(Host.id.in_(body.host_ids))
    hosts = (await db.execute(stmt)).scalars().all()

    tasks: list[TaskOut] = []
    skipped: list[AgentRolloutSkip] = []
    for host in hosts:
        if not agent_connected(host):
            skipped.append(AgentRolloutSkip(host_id=host.id, host_name=host.name, reason="offline"))
            continue
        if host.agent_version == binary.version:
            skipped.append(
                AgentRolloutSkip(host_id=host.id, host_name=host.name, reason="already up to date")
            )
            continue
        try:
            task = await request_agent_upgrade(
                db, host, binary=binary, requested_by=user.email
            )
            tasks.append(task_out(task))
        except ApiError as exc:
            skipped.append(
                AgentRolloutSkip(host_id=host.id, host_name=host.name, reason=exc.message)
            )

    return AgentRolloutResult(tasks=tasks, skipped=skipped)

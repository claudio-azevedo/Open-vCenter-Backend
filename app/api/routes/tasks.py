from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...models import Task, Vm
from ...schemas import TaskDetailOut, TaskOut
from ...services.rbac import vm_visible
from ...services.serializers import task_detail_out, task_out
from ..deps import DbSession, Scope
from ..errors import forbidden, not_found

router = APIRouter(tags=["tasks"])


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    db: DbSession,
    scope: Scope,
    vm_id: str | None = Query(default=None, alias="vmId"),
    host_id: str | None = Query(default=None, alias="hostId"),
    status_: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TaskOut]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if vm_id is not None:
        stmt = stmt.where(Task.target_id == vm_id)
    if host_id is not None:
        stmt = stmt.where(Task.host_id == host_id)
    if status_ is not None:
        stmt = stmt.where(Task.status == status_)

    tasks = (await db.execute(stmt)).scalars().all()
    if scope.all:
        return [task_out(t) for t in tasks]

    visible: list[Task] = []
    for t in tasks:
        if t.target_type == "host" and scope.sees_host(t.host_id):
            visible.append(t)
        elif t.target_type == "vm":
            vm = await db.get(Vm, t.target_id)
            if vm is not None and vm_visible(scope, vm):
                visible.append(t)
            elif scope.sees_host(t.host_id):
                visible.append(t)
    return [task_out(t) for t in visible]


@router.get("/tasks/{task_id}", response_model=TaskDetailOut)
async def get_task(task_id: str, db: DbSession, scope: Scope) -> TaskDetailOut:
    task = await db.get(Task, task_id)
    if task is None:
        raise not_found("Task")
    if not scope.all and not scope.sees_host(task.host_id):
        raise forbidden()
    return task_detail_out(task)

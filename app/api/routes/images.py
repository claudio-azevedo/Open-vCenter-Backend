from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ...models import Iso, Template
from ...schemas import IsoOut, TemplateOut
from ...services.serializers import iso_out, template_out
from ..deps import DbSession, Scope

router = APIRouter(tags=["images"])


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(db: DbSession, scope: Scope) -> list[TemplateOut]:
    rows = (await db.execute(select(Template).order_by(Template.name))).scalars().all()
    return [template_out(t) for t in rows if scope.sees_host(t.host_id)]


@router.get("/isos", response_model=list[IsoOut])
async def list_isos(db: DbSession, scope: Scope) -> list[IsoOut]:
    rows = (await db.execute(select(Iso).order_by(Iso.name))).scalars().all()
    return [iso_out(i) for i in rows if scope.sees_host(i.host_id)]

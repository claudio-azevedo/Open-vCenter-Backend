from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ...cache import get_cache
from ...db import session_scope
from ...messaging import rabbit_healthy
from ...schemas import HealthOut

router = APIRouter(tags=["health"])


async def _db_ok() -> bool:
    try:
        async with session_scope() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def _cache_ok() -> bool:
    try:
        return bool(await get_cache().ping())
    except Exception:  # noqa: BLE001
        return False


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    db = await _db_ok()
    cache = await _cache_ok()
    rabbit = await rabbit_healthy()
    return HealthOut(ok=db and cache and rabbit, db=db, cache=cache, rabbit=rabbit)

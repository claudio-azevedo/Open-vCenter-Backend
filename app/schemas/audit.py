from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import CamelModel


class AuditEventOut(CamelModel):
    id: str
    occurred_at: datetime
    actor_type: str
    actor_email: str | None = None
    actor_name: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    target_name: str | None = None
    host_id: str | None = None
    cluster_id: str | None = None
    task_id: str | None = None
    outcome: str
    error: str | None = None
    # free-form, camelCase keys (request params, or {before, after} for an update)
    details: dict[str, Any] | None = None


class AuditEventPage(CamelModel):
    items: list[AuditEventOut]
    # pass back as `cursor` for the next (older) page; null on the last page
    next_cursor: str | None = None

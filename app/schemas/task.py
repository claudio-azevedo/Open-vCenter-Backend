from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import CamelModel


class TaskOut(CamelModel):
    id: str
    kind: str
    status: str
    target_type: str
    target_id: str
    target_name: str | None = None
    requested_by: str
    progress: int | None = None
    progress_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: str | None = None
    error: str | None = None
    correlation_id: str | None = None


class TaskDetailOut(TaskOut):
    """Single-task view: also carries the raw agent request / response envelopes
    for the "Details" dialog. Omitted from list endpoints to keep them lean."""

    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None


class TaskEnvelope(CamelModel):
    """202 response body for VM actions."""

    task: TaskOut

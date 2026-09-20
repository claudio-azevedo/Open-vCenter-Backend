from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base, TimestampMixin

TASK_STATUSES = ("queued", "running", "succeeded", "failed", "timeout")
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "timeout"})


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    # e.g. "vm_start", "vm_delete", "host_hwinventory"
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")

    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # vm | host
    target_id: Mapped[str] = mapped_column(UUID_STR, nullable=False, index=True)
    target_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # host the request was routed through (for filtering by host)
    host_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # last human-readable step reported by the agent while status == "running"
    progress_message: Mapped[str | None] = mapped_column(String(255), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Full AgentRequest envelope published to <hostid>.request (function, params,
    # requested_by, issued_at) and the latest AgentResponse envelope received on
    # <hostid>.response. Kept verbatim for the task "Details" view / debugging.
    request_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # AMQP message id used on <hostid>.request / matched on <hostid>.response
    correlation_id: Mapped[str | None] = mapped_column(
        UUID_STR, nullable=True, index=True
    )

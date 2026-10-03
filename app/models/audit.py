from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base

# `actor_type`: a signed-in user, or the backend itself (worker-side changes such
# as a VM dropped from the inventory because its host stopped reporting it).
AUDIT_ACTOR_TYPES = ("user", "system")

# `outcome`: a DB-only change is committed together with its event, so it is
# `succeeded` from the start. An agent task starts `pending` and the worker
# settles it to the task's terminal status.
AUDIT_OUTCOMES = ("pending", "succeeded", "failed", "timeout")


class AuditEvent(Base):
    """Who changed what, and when. Append-only: rows are never edited, except a
    `pending` event's `outcome` / `error` once its task finishes, and they are
    pruned only by the worker's `OVC_AUDIT_RETENTION_DAYS` sweeper.

    No FKs on purpose - an event must outlive the VM / host / folder it names,
    so the target's name is captured at event time (`target_name`) and
    `host_id` / `cluster_id` / `task_id` are plain ids."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    # null for actor_type == "system"
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # "<target_type>.<verb>", e.g. "vm.delete", "vm.move", "folder.rename"
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # vm | host | cluster | folder | vlan | tag | tag_category | agent_binary
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # null only for actions without a single target (e.g. releasing every VM lock)
    target_id: Mapped[str | None] = mapped_column(UUID_STR, nullable=True, index=True)
    target_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # where the target lived at event time - lets a host / cluster view list the
    # events of everything under it
    host_id: Mapped[str | None] = mapped_column(UUID_STR, nullable=True, index=True)
    cluster_id: Mapped[str | None] = mapped_column(UUID_STR, nullable=True, index=True)

    # the agent task this event queued, if any
    task_id: Mapped[str | None] = mapped_column(UUID_STR, nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # action-specific context, camelCase keys: request params, or
    # {"before": {...}, "after": {...}} for an update
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

"""Audit trail: who changed what, and when (`models.AuditEvent`).

Routes call :func:`record` right after the change they audit, on the request's
session, so the event commits - or rolls back - together with the change itself.
An event for an agent task starts ``pending``; the worker settles it to the
task's terminal status (:func:`settle_task_events`).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.errors import ApiError
from ..models import (
    AgentBinary,
    AuditEvent,
    Cluster,
    Folder,
    Host,
    Tag,
    TagCategory,
    Task,
    User,
    Vlan,
    Vm,
)

# Shown as the actor of worker-side events.
SYSTEM_ACTOR_NAME = "Open vCenter"

Target = Vm | Host | Cluster | Folder | Vlan | Tag | TagCategory | AgentBinary


@dataclass(frozen=True)
class _TargetRef:
    type: str
    id: str | None
    name: str | None
    host_id: str | None = None
    cluster_id: str | None = None


def _ref(target: Target) -> _TargetRef:
    if isinstance(target, Vm):
        return _TargetRef("vm", target.id, target.name, target.host_id, target.cluster_id)
    if isinstance(target, Host):
        return _TargetRef("host", target.id, target.name, target.id, target.cluster_id)
    if isinstance(target, Cluster):
        return _TargetRef("cluster", target.id, target.name, None, target.id)
    if isinstance(target, Folder):
        return _TargetRef(
            "folder", target.id, target.name, target.host_id, target.cluster_id
        )
    if isinstance(target, Vlan):
        return _TargetRef("vlan", target.id, target.name, target.host_id, target.cluster_id)
    if isinstance(target, Tag):
        return _TargetRef("tag", target.id, target.name)
    if isinstance(target, TagCategory):
        return _TargetRef("tag_category", target.id, target.name)
    if isinstance(target, AgentBinary):
        return _TargetRef("agent_binary", target.id, target.version)
    raise TypeError(f"not an auditable target: {type(target).__name__}")


async def record(
    db: AsyncSession,
    actor: User | None,
    action: str,
    target: Target | None = None,
    *,
    target_type: str | None = None,
    task: Task | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Add one event to the session. ``actor=None`` records a system event.

    ``target`` must still be loaded - record a delete *before* deleting the row.
    Without a target (a bulk action) pass ``target_type``."""
    ref = _ref(target) if target is not None else _TargetRef(target_type or "", None, None)
    if not ref.type:
        raise ValueError("record() needs a target or a target_type")
    event = AuditEvent(
        actor_type="user" if actor is not None else "system",
        actor_email=actor.email if actor is not None else None,
        actor_name=(actor.display_name or actor.email) if actor is not None else SYSTEM_ACTOR_NAME,
        action=action,
        target_type=ref.type,
        target_id=ref.id,
        target_name=ref.name,
        host_id=ref.host_id,
        cluster_id=ref.cluster_id,
        task_id=task.id if task is not None else None,
        outcome="pending" if task is not None else "succeeded",
        details=details or None,
    )
    db.add(event)
    await db.flush()
    return event


def changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    """``{"before": {...}, "after": {...}}`` with only the keys whose value
    changed, or None when nothing did (the caller then records nothing)."""
    changed = [k for k in after if before.get(k) != after[k]]
    if not changed:
        return None
    return {
        "before": {k: before.get(k) for k in changed},
        "after": {k: after[k] for k in changed},
    }


async def settle_task_events(
    db: AsyncSession, task_ids: Iterable[str], outcome: str, error: str | None
) -> None:
    """Stamp the terminal task status on the events still ``pending`` for it."""
    ids = list(task_ids)
    if not ids:
        return
    await db.execute(
        update(AuditEvent)
        .where(AuditEvent.task_id.in_(ids))
        .where(AuditEvent.outcome == "pending")
        .values(outcome=outcome, error=error)
    )


async def prune_events(db: AsyncSession, retention_days: int) -> int:
    """Drop events older than ``retention_days``; 0 keeps everything."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await db.execute(delete(AuditEvent).where(AuditEvent.occurred_at < cutoff))
    return result.rowcount or 0


# ---- listing ------------------------------------------------------------------

def encode_cursor(event: AuditEvent) -> str:
    raw = f"{event.occurred_at.isoformat()}|{event.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, event_id = raw.split("|", 1)
        occurred_at = datetime.fromisoformat(ts)
        event_id = str(UUID(event_id))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ApiError("INVALID", "Invalid audit cursor") from exc
    if occurred_at.tzinfo is None:
        raise ApiError("INVALID", "Invalid audit cursor")
    return occurred_at, event_id


@dataclass
class AuditFilter:
    actor: str | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    host_id: str | None = None
    cluster_id: str | None = None
    outcome: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    q: str | None = None


async def list_events(
    db: AsyncSession, flt: AuditFilter, *, limit: int, cursor: str | None
) -> tuple[list[AuditEvent], str | None]:
    """Newest first, keyset-paginated on (occurred_at, id). Returns the page and
    the cursor for the next one (None on the last page)."""
    stmt = select(AuditEvent)
    if flt.actor:
        like = f"%{flt.actor}%"
        stmt = stmt.where(
            or_(AuditEvent.actor_email.ilike(like), AuditEvent.actor_name.ilike(like))
        )
    if flt.action:
        stmt = stmt.where(AuditEvent.action == flt.action)
    if flt.target_type:
        stmt = stmt.where(AuditEvent.target_type == flt.target_type)
    if flt.target_id:
        stmt = stmt.where(AuditEvent.target_id == flt.target_id)
    if flt.host_id:
        stmt = stmt.where(AuditEvent.host_id == flt.host_id)
    if flt.cluster_id:
        stmt = stmt.where(AuditEvent.cluster_id == flt.cluster_id)
    if flt.outcome:
        stmt = stmt.where(AuditEvent.outcome == flt.outcome)
    if flt.since:
        stmt = stmt.where(AuditEvent.occurred_at >= flt.since)
    if flt.until:
        stmt = stmt.where(AuditEvent.occurred_at < flt.until)
    if flt.q:
        like = f"%{flt.q}%"
        stmt = stmt.where(
            or_(
                AuditEvent.target_name.ilike(like),
                AuditEvent.actor_email.ilike(like),
                AuditEvent.actor_name.ilike(like),
            )
        )
    if cursor:
        occurred_at, event_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(AuditEvent.occurred_at, AuditEvent.id) < (occurred_at, event_id)
        )

    rows = list(
        (
            await db.execute(
                stmt.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(rows[-1])
    return rows, None

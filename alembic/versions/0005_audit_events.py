"""audit events

Adds `audit_events`: who changed what and when (VM delete / move / power,
folder and cluster changes, host membership, tags, VLANs, agent binaries). No
FKs - an event outlives the object it names. Pruned by the worker after
`OVC_AUDIT_RETENTION_DAYS`.

Revision ID: 0005_audit_events
Revises: 0004_vm_guest_os_thumbnails
Create Date: 2026-10-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_audit_events"
down_revision: str | None = "0004_vm_guest_os_thumbnails"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED = (
    "occurred_at",
    "actor_email",
    "action",
    "target_id",
    "host_id",
    "cluster_id",
    "task_id",
)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_email", sa.String(length=255), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("target_name", sa.String(length=255), nullable=True),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("cluster_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("task_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    for column in _INDEXED:
        op.create_index(
            op.f(f"ix_audit_events_{column}"), "audit_events", [column], unique=False
        )


def downgrade() -> None:
    for column in _INDEXED:
        op.drop_index(op.f(f"ix_audit_events_{column}"), table_name="audit_events")
    op.drop_table("audit_events")

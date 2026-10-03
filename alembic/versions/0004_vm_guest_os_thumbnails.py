"""vm guest OS and console thumbnails

Adds `vms.guest_os` (guest OS name from the hypervisor's guest integration) and
the `vm_thumbnails` table (last console JPEG per VM, captured by the agent's
vm_inventory for Running VMs).

Revision ID: 0004_vm_guest_os_thumbnails
Revises: 0003_tag_color
Create Date: 2026-10-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_vm_guest_os_thumbnails"
down_revision: str | None = "0003_tag_color"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vms", sa.Column("guest_os", sa.String(length=255), nullable=True))

    op.create_table(
        "vm_thumbnails",
        sa.Column("vm_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=32),
            server_default="image/jpeg",
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["vm_id"],
            ["vms.id"],
            name=op.f("fk_vm_thumbnails_vm_id_vms"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("vm_id", name=op.f("pk_vm_thumbnails")),
    )


def downgrade() -> None:
    op.drop_table("vm_thumbnails")
    op.drop_column("vms", "guest_os")

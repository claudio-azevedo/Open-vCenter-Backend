"""VM tags and tag categories

Global tag catalog: `tag_categories`, `tags` (standalone or in one category) and
the `vm_tags` assignment table. A VM carries at most one tag per category - that
rule lives in services.tags, not in the schema.

Revision ID: 0002_tags
Revises: 0001_initial
Create Date: 2026-09-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_tags"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "tag_categories",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tag_categories")),
    )
    # names are unique case-insensitively
    op.create_index(
        "uq_tag_categories_name",
        "tag_categories",
        [sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "tags",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("category_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["tag_categories.id"],
            name=op.f("fk_tags_category_id_tag_categories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
    )
    op.create_index(op.f("ix_tags_category_id"), "tags", ["category_id"], unique=False)
    # unique within a category; standalone tags unique among themselves
    op.create_index(
        "uq_tags_category_name",
        "tags",
        ["category_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("category_id IS NOT NULL"),
    )
    op.create_index(
        "uq_tags_standalone_name",
        "tags",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("category_id IS NULL"),
    )

    op.create_table(
        "vm_tags",
        sa.Column("vm_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tag_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["vm_id"], ["vms.id"], name=op.f("fk_vm_tags_vm_id_vms"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tags.id"], name=op.f("fk_vm_tags_tag_id_tags"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("vm_id", "tag_id", name=op.f("pk_vm_tags")),
    )
    op.create_index(op.f("ix_vm_tags_tag_id"), "vm_tags", ["tag_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_vm_tags_tag_id"), table_name="vm_tags")
    op.drop_table("vm_tags")
    op.drop_index("uq_tags_standalone_name", table_name="tags")
    op.drop_index("uq_tags_category_name", table_name="tags")
    op.drop_index(op.f("ix_tags_category_id"), table_name="tags")
    op.drop_table("tags")
    op.drop_index("uq_tag_categories_name", table_name="tag_categories")
    op.drop_table("tag_categories")

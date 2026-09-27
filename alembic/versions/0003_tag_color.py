"""tag colour

Adds `tags.color`: a name from the fixed palette in models.tag.TAG_COLORS
(existing tags become "gray").

Revision ID: 0003_tag_color
Revises: 0002_tags
Create Date: 2026-09-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_tag_color"
down_revision: str | None = "0002_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tags",
        sa.Column("color", sa.String(length=16), server_default="gray", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tags", "color")

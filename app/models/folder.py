from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base, TimestampMixin


class Folder(Base, TimestampMixin):
    """A logical VM folder, created and managed by the application (not the agent).

    A folder is scoped to exactly one of:
      - a cluster  → holds VMs from any host in that cluster
      - a standalone host → holds VMs on that host
    Flat, one level (no nesting) in milestone 2.
    """

    __tablename__ = "folders"
    __table_args__ = (
        CheckConstraint(
            "(host_id IS NOT NULL AND cluster_id IS NULL) "
            "OR (host_id IS NULL AND cluster_id IS NOT NULL)",
            name="folder_scope_exactly_one",
        ),
    )

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    cluster_id: Mapped[str | None] = mapped_column(
        UUID_STR, ForeignKey("clusters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    host_id: Mapped[str | None] = mapped_column(
        UUID_STR, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base, TimestampMixin


class Vlan(Base, TimestampMixin):
    """An 802.1Q VLAN definition, created and managed by the application (not the
    agent). Like :class:`Folder`, a VLAN is scoped to exactly one of:

      - a cluster  → usable by any host in that cluster
      - a standalone host

    A VM NIC references a VLAN only by its numeric ``vlan_id`` tag (1-4094); the
    tag is what the agent applies to the adapter.
    """

    __tablename__ = "vlans"
    __table_args__ = (
        CheckConstraint(
            "(host_id IS NOT NULL AND cluster_id IS NULL) "
            "OR (host_id IS NULL AND cluster_id IS NOT NULL)",
            name="vlan_scope_exactly_one",
        ),
        CheckConstraint(
            "vlan_id BETWEEN 1 AND 4094",
            name="vlan_tag_range",
        ),
        # the same tag must not be defined twice within one scope
        Index(
            "uq_vlans_cluster_tag",
            "cluster_id",
            "vlan_id",
            unique=True,
            postgresql_where=text("cluster_id IS NOT NULL"),
        ),
        Index(
            "uq_vlans_host_tag",
            "host_id",
            "vlan_id",
            unique=True,
            postgresql_where=text("host_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    cluster_id: Mapped[str | None] = mapped_column(
        UUID_STR,
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    host_id: Mapped[str | None] = mapped_column(
        UUID_STR,
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base, TimestampMixin
from .host import DEFAULT_HYPERVISOR


class Cluster(Base, TimestampMixin):
    __tablename__ = "clusters"

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # a cluster is hypervisor-homogeneous; its member hosts must match
    hypervisor: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=DEFAULT_HYPERVISOR.value
    )

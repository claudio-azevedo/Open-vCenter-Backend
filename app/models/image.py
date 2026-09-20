from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import UUID_STR, Base, TimestampMixin


class Template(Base, TimestampMixin):
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    host_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Provisioned size: sum of each disk's virtual/max size.
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Real on-disk footprint of the exported (Dynamic-disk) template folder.
    disk_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provisioned sizing read from the exported .vmcx (Compare-VM).
    cpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memory_mb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    guest_os: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Iso(Base, TimestampMixin):
    __tablename__ = "isos"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    host_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # MD5 of the file content, reported by the agent. Not unique: the same ISO
    # legitimately exists on multiple hosts. Lets the UI spot a name collision
    # where two hosts carry different builds under the same file name.
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

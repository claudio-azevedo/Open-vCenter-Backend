from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base
from .host import DEFAULT_HYPERVISOR


class AgentBinary(Base):
    """An uploaded ovc-agent build, available for host onboarding bundles and
    for the ``host_update_agent`` self-upgrade. The bytes live in the configured
    store (``app/services/agent_store.py``); this row is the catalogue entry.

    One row per (version, hypervisor). ``is_active`` marks the build a new host
    bundle ships and that ``rollout`` targets - at most one active row per
    hypervisor (enforced by a partial unique index)."""

    __tablename__ = "agent_binaries"
    __table_args__ = (
        UniqueConstraint("version", "hypervisor", name="uq_agent_binaries_version_hypervisor"),
    )

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    # Operator-supplied. MUST match the agent's own AgentVersion constant
    # (internal/tasks/agent_status.go) - the agent rejects an upgrade whose
    # version equals its running version, and the string is compared verbatim.
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    # which agent build this is - mirrors Host.hypervisor; only "hyperv" today
    hypervisor: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=DEFAULT_HYPERVISOR.value
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="ovc-agent.exe")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/octet-stream"
    )
    # "local" | "s3" - the backend that held the bytes at upload time (audit)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    # object key inside that store: "<id>/<filename>"
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

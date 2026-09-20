from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import UUID_STR, Base

# Quick metrics are a short rolling time-series: the agent samples every few
# minutes, the worker keeps only the last hour (see app.services.inventory and
# app.worker.main::_metrics_retention_sweeper). Not a monitoring stack.


class HostMetric(Base):
    __tablename__ = "host_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # sample time, from the agent's reported_at (falls back to ingest time)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_rx_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    net_tx_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # full payload - per-disk / per-nic breakdown for the UI
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class VmMetric(Base):
    __tablename__ = "vm_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    vm_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("vms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    # best-effort percent; raw Hyper-V metering numbers live in `detail`
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    net_rx_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    net_tx_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

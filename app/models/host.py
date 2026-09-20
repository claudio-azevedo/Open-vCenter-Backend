from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_host_short_id, new_id
from .base import UUID_STR, Base, TimestampMixin


class Hypervisor(StrEnum):
    """Which virtualization backend a host/cluster runs. Today only Hyper-V is
    implemented; `LIBVIRT` / `KVM` land when their agents do."""

    HYPERV = "hyperv"


DEFAULT_HYPERVISOR = Hypervisor.HYPERV


class Host(Base, TimestampMixin):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    # Short opaque handle used in RabbitMQ queue names / agent user / logs / the
    # agent config.ini. The UUID `id` stays the PK and the FK target everywhere.
    short_id: Mapped[str] = mapped_column(
        String(16), unique=True, nullable=False, default=new_host_short_id
    )
    cluster_id: Mapped[str | None] = mapped_column(
        UUID_STR,
        ForeignKey("clusters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # fqdn / ip_address are resolved by the agent and reported on agent_status -
    # not asked for at registration. Nullable until the agent first checks in.
    fqdn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # virtualization backend; drives which agent build runs on the host
    hypervisor: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=DEFAULT_HYPERVISOR.value
    )

    # RabbitMQ credentials this host's agent uses. Provisioned by the startup
    # preflight / create_host against the management API; the agent's .ini gets
    # amqp://<user>:<password>@host/. Password is stored as-is (dev) - encrypt
    # before any real deployment.
    agent_rmq_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_rmq_password: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # agent status (updated by the worker from <hostid>.agent_status)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_refresh_vm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_refresh_host: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # hardware inventory (from <hostid>.host_inventory / host_hwinventory)
    hardware: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

from __future__ import annotations

from datetime import datetime

from ..models import DEFAULT_HYPERVISOR
from .common import CamelModel
from .task import TaskOut


class AgentBinaryOut(CamelModel):
    id: str
    version: str
    hypervisor: str = DEFAULT_HYPERVISOR.value
    filename: str
    size_bytes: int
    checksum_sha256: str
    content_type: str
    storage_backend: str
    notes: str | None = None
    is_active: bool
    uploaded_by: str
    created_at: datetime


class AgentBinaryUpdate(CamelModel):
    """PATCH - any subset. Setting ``isActive`` true demotes the previous active
    build for the same hypervisor."""

    notes: str | None = None
    is_active: bool | None = None


class AgentStorageInfoOut(CamelModel):
    """Read-only view of where agent binaries are stored. The backend is chosen
    by ``OVC_AGENT_STORAGE`` and cannot be changed from the UI."""

    backend: str  # "local" | "s3"
    location: str  # the directory, or "bucket/prefix"
    download_url_ttl_seconds: int
    # local mode only: false when OVC_PUBLIC_BASE_URL is unset (upgrades blocked)
    downloads_enabled: bool


class AgentUpgradeRequest(CamelModel):
    # omitted ⇒ the active binary for the host's hypervisor
    binary_id: str | None = None


class AgentRolloutRequest(CamelModel):
    # empty / omitted ⇒ every online host on the binary's hypervisor whose agent
    # version differs from this build
    host_ids: list[str] | None = None


class AgentRolloutSkip(CamelModel):
    host_id: str
    host_name: str
    reason: str


class AgentRolloutResult(CamelModel):
    tasks: list[TaskOut]
    skipped: list[AgentRolloutSkip]

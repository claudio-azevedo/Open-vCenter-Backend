from __future__ import annotations

from datetime import datetime

from .common import CamelModel


class TemplateOut(CamelModel):
    id: str
    host_id: str
    name: str
    path: str
    size_bytes: int
    disk_size_bytes: int = 0
    notes: str | None = None
    cpu_count: int = 0
    memory_mb: int = 0
    guest_os: str | None = None
    created_at: datetime | None = None


class IsoOut(CamelModel):
    id: str
    host_id: str
    name: str
    path: str
    size_bytes: int
    checksum: str | None = None

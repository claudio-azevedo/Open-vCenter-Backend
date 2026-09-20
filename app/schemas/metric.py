from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import CamelModel


class HostMetricSample(CamelModel):
    ts: datetime
    cpu_percent: float | None = None
    mem_percent: float | None = None
    disk_latency_ms: float | None = None
    net_rx_bps: int | None = None
    net_tx_bps: int | None = None
    detail: dict[str, Any] | None = None


class VmMetricSample(CamelModel):
    ts: datetime
    cpu_percent: float | None = None
    mem_bytes: int | None = None
    disk_bytes: int | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None
    detail: dict[str, Any] | None = None

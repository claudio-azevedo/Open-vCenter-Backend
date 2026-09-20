from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ..models import DEFAULT_HYPERVISOR, Hypervisor
from .common import CamelModel


class RefreshIntervals(CamelModel):
    vm: int
    host: int


class HostAgentStatus(CamelModel):
    version: str | None = None
    last_seen: datetime | None = None
    connected: bool = False
    refresh_intervals: RefreshIntervals | None = None


class CpuInfo(CamelModel):
    model: str = ""
    sockets: int = 0
    cores: int = 0
    logical: int = 0


class StorageVolume(CamelModel):
    path: str
    label: str | None = None
    total_bytes: int = 0
    free_bytes: int = 0


class OsInfo(CamelModel):
    caption: str = ""
    version: str = ""


class NetworkAdapter(CamelModel):
    name: str
    mac: str
    speed_bps: int = 0
    connected: bool = False
    description: str = ""


class VirtualSwitch(CamelModel):
    """A Hyper-V virtual switch (or the equivalent bridge on other hypervisors)."""

    name: str
    # "External" | "Internal" | "Private"
    type: str = ""
    # the physical uplink adapter, for External switches
    net_adapter: str | None = None


class SystemInfo(CamelModel):
    manufacturer: str = ""
    model: str = ""


class LoadSnapshot(CamelModel):
    """CPU / memory utilisation at the last hardware refresh (not live metrics)."""

    cpu_percent: float | None = None
    memory_percent: float | None = None


class FailoverCluster(CamelModel):
    """The Windows Server Failover Cluster the host belongs to - distinct from the
    application-level `clusterId` grouping."""

    clustered: bool = False
    name: str | None = None
    state: str = ""
    nodes: list[str] = Field(default_factory=list)


class HyperVPaths(CamelModel):
    default_vm_path: str | None = None
    default_vhd_path: str | None = None


class HostHardwareInventory(CamelModel):
    cpu: CpuInfo = Field(default_factory=CpuInfo)
    memory_bytes: int = 0
    storage: list[StorageVolume] = Field(default_factory=list)
    os: OsInfo = Field(default_factory=OsInfo)
    # physical host NICs (every adapter, connected or not)
    network: list[NetworkAdapter] = Field(default_factory=list)
    # virtual switches / bridges defined on the host
    v_switches: list[VirtualSwitch] = Field(default_factory=list)
    system: SystemInfo | None = None
    boot_time: datetime | None = None
    load: LoadSnapshot | None = None
    cluster: FailoverCluster | None = None
    hyperv: HyperVPaths | None = None


class HostOut(CamelModel):
    id: str
    # short opaque handle used in RabbitMQ queue names / the agent config.ini
    short_id: str
    cluster_id: str | None = None
    name: str
    # agent-resolved; null until the agent first reports
    fqdn: str | None = None
    ip_address: str | None = None
    hypervisor: str = DEFAULT_HYPERVISOR.value
    online: bool
    agent: HostAgentStatus
    vm_count: int


class HostDetailOut(HostOut):
    hardware: HostHardwareInventory | None = None


class HostAgentConfigOut(CamelModel):
    """Everything needed to bring a freshly-registered host's agent online:
    its id, the AMQP URL with per-host credentials, and a ready-to-edit
    `config.ini`. Admin-only - it carries RabbitMQ credentials."""

    host_id: str
    rabbitmq_url: str
    config_ini: str
    filename: str = "config.ini"


class HostAgentInstallOut(CamelModel):
    """A tokenized URL for this host's ``install.ps1`` plus a ready-to-paste
    elevated-PowerShell one-liner that downloads and runs it. Admin-only - the
    URL's HMAC token stands in for a login, so anyone with it can pull the
    script (which embeds RabbitMQ credentials) until it expires."""

    url: str
    command: str
    expires_at: datetime
    filename: str


class HostCreate(CamelModel):
    name: str
    cluster_id: str | None = None
    hypervisor: Hypervisor = DEFAULT_HYPERVISOR


class HostUpdate(CamelModel):
    """PATCH - any subset of:
    - name: rename the host record
    - fqdn: override the agent-resolved FQDN (normally left to the agent)
    - clusterId: a cluster id to join, or null to make the host standalone

    Only fields actually present in the request body are applied.
    """

    name: str | None = None
    fqdn: str | None = None
    cluster_id: str | None = None

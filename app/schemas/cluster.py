from __future__ import annotations

from ..models import DEFAULT_HYPERVISOR, Hypervisor
from .common import CamelModel


class ClusterOut(CamelModel):
    id: str
    name: str
    hypervisor: str = DEFAULT_HYPERVISOR.value
    host_count: int
    vm_count: int


class ClusterCreate(CamelModel):
    name: str
    hypervisor: Hypervisor = DEFAULT_HYPERVISOR


class ClusterUpdate(CamelModel):
    name: str

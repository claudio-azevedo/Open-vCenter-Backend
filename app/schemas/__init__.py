from __future__ import annotations

from .agent_binary import (
    AgentBinaryOut,
    AgentBinaryUpdate,
    AgentRolloutRequest,
    AgentRolloutResult,
    AgentRolloutSkip,
    AgentStorageInfoOut,
)
from .auth import HealthOut, MeOut, ScopeOut
from .cluster import ClusterCreate, ClusterOut, ClusterUpdate
from .common import CamelModel, ErrorDetail, ErrorResponse
from .folder import FolderCreate, FolderOut, FolderUpdate
from .host import (
    HostAgentConfigOut,
    HostAgentInstallOut,
    HostAgentStatus,
    HostCreate,
    HostDetailOut,
    HostHardwareInventory,
    HostOut,
    HostUpdate,
)
from .image import IsoOut, TemplateOut
from .metric import HostMetricSample, VmMetricSample
from .task import TaskDetailOut, TaskEnvelope, TaskOut
from .vlan import VlanCreate, VlanOut, VlanUpdate
from .vm import (
    VmActionRequest,
    VmClone,
    VmCreate,
    VmCreateEnvelope,
    VmDisk,
    VmDiskSpec,
    VmLock,
    VmLockEntry,
    VmMemory,
    VmMove,
    VmNic,
    VmOut,
    VmSnapshot,
)

__all__ = [
    "AgentBinaryOut",
    "AgentBinaryUpdate",
    "AgentRolloutRequest",
    "AgentRolloutResult",
    "AgentRolloutSkip",
    "AgentStorageInfoOut",
    "CamelModel",
    "ErrorDetail",
    "ErrorResponse",
    "HealthOut",
    "MeOut",
    "ScopeOut",
    "ClusterOut",
    "ClusterCreate",
    "ClusterUpdate",
    "FolderOut",
    "FolderCreate",
    "FolderUpdate",
    "HostOut",
    "HostDetailOut",
    "HostCreate",
    "HostUpdate",
    "HostAgentConfigOut",
    "HostAgentInstallOut",
    "HostAgentStatus",
    "HostHardwareInventory",
    "TemplateOut",
    "IsoOut",
    "HostMetricSample",
    "VmMetricSample",
    "TaskOut",
    "TaskDetailOut",
    "TaskEnvelope",
    "VlanOut",
    "VlanCreate",
    "VlanUpdate",
    "VmOut",
    "VmMove",
    "VmActionRequest",
    "VmClone",
    "VmCreate",
    "VmCreateEnvelope",
    "VmSnapshot",
    "VmLock",
    "VmLockEntry",
    "VmMemory",
    "VmDisk",
    "VmDiskSpec",
    "VmNic",
]

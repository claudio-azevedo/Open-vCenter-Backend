from __future__ import annotations

from pydantic import Field, model_validator

from .common import CamelModel


class VlanOut(CamelModel):
    id: str
    name: str
    vlan_id: int
    description: str | None = None
    is_default: bool = False
    cluster_id: str | None = None
    host_id: str | None = None


class VlanCreate(CamelModel):
    name: str = Field(min_length=1, max_length=255)
    vlan_id: int = Field(ge=1, le=4094)
    description: str | None = None
    is_default: bool = False
    cluster_id: str | None = None
    host_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> VlanCreate:
        if bool(self.cluster_id) == bool(self.host_id):
            raise ValueError("provide exactly one of clusterId or hostId")
        return self


class VlanUpdate(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_default: bool | None = None

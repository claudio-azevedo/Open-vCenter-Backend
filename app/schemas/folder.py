from __future__ import annotations

from pydantic import model_validator

from .common import CamelModel


class FolderOut(CamelModel):
    id: str
    name: str
    cluster_id: str | None = None
    host_id: str | None = None


class FolderCreate(CamelModel):
    name: str
    cluster_id: str | None = None
    host_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> FolderCreate:
        if bool(self.cluster_id) == bool(self.host_id):
            raise ValueError("provide exactly one of clusterId or hostId")
        return self


class FolderUpdate(CamelModel):
    name: str

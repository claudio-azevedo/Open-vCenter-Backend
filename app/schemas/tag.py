from __future__ import annotations

from .common import CamelModel


class TagCategoryOut(CamelModel):
    id: str
    name: str


class TagCategoryCreate(CamelModel):
    name: str


class TagCategoryUpdate(CamelModel):
    name: str


class TagOut(CamelModel):
    id: str
    name: str
    # null = a standalone tag
    category_id: str | None = None
    # a palette name (models.tag.TAG_COLORS)
    color: str = "gray"
    # VMs carrying the tag, counted within the caller's visible scope
    vm_count: int = 0


class TagCreate(CamelModel):
    name: str
    category_id: str | None = None
    color: str = "gray"


class TagUpdate(CamelModel):
    """PATCH - any subset of `name`, `categoryId` (null = make it standalone) and
    `color`. Only fields present in the request body are applied."""

    name: str | None = None
    category_id: str | None = None
    color: str | None = None


class VmTagsUpdate(CamelModel):
    """PUT /vms/{id}/tags - the VM's complete tag set (replaces the current one)."""

    tag_ids: list[str]

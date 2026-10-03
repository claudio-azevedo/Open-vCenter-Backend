from __future__ import annotations

from fastapi import APIRouter, Response, status

from ...models import Tag, TagCategory
from ...schemas import (
    TagCategoryCreate,
    TagCategoryOut,
    TagCategoryUpdate,
    TagCreate,
    TagOut,
    TagUpdate,
)
from ...services.audit import changes, record
from ...services.serializers import tag_category_out, tag_out
from ...services.tags import (
    create_category,
    create_tag,
    delete_category,
    delete_tag,
    list_categories,
    list_tags,
    rename_category,
    tag_vm_count,
    update_tag,
)
from ..deps import CurrentUser, DbSession, Scope
from ..errors import forbidden, not_found

router = APIRouter(tags=["tags"])

# The catalog is global: any signed-in user reads it (to render a VM's tags) and
# assigns tags to the VMs they can see (PUT /vms/{id}/tags). Only an administrator
# creates, renames or deletes tags and categories.


def _require_admin(scope: Scope) -> None:
    if not scope.all:
        raise forbidden("Only an administrator can manage tags")


# ---- categories ---------------------------------------------------------------

@router.get("/tag-categories", response_model=list[TagCategoryOut])
async def get_tag_categories(db: DbSession, scope: Scope) -> list[TagCategoryOut]:
    return [tag_category_out(c) for c in await list_categories(db)]


@router.post(
    "/tag-categories", response_model=TagCategoryOut, status_code=status.HTTP_201_CREATED
)
async def add_tag_category(
    body: TagCategoryCreate, db: DbSession, scope: Scope, user: CurrentUser
) -> TagCategoryOut:
    _require_admin(scope)
    category = await create_category(db, body.name)
    await record(db, user, "tag_category.create", category)
    return tag_category_out(category)


async def _admin_category(db: DbSession, scope: Scope, category_id: str) -> TagCategory:
    _require_admin(scope)
    category = await db.get(TagCategory, category_id)
    if category is None:
        raise not_found("Tag category")
    return category


@router.patch("/tag-categories/{category_id}", response_model=TagCategoryOut)
async def patch_tag_category(
    category_id: str, body: TagCategoryUpdate, db: DbSession, scope: Scope, user: CurrentUser
) -> TagCategoryOut:
    category = await _admin_category(db, scope, category_id)
    before = category.name
    category = await rename_category(db, category, body.name)
    if diff := changes({"name": before}, {"name": category.name}):
        await record(db, user, "tag_category.rename", category, details=diff)
    return tag_category_out(category)


@router.delete("/tag-categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tag_category(
    category_id: str, db: DbSession, scope: Scope, user: CurrentUser
) -> Response:
    """Delete the category and all of its tags (they are removed from every VM)."""
    category = await _admin_category(db, scope, category_id)
    await record(db, user, "tag_category.delete", category)
    await delete_category(db, category)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- tags -----------------------------------------------------------------------

@router.get("/tags", response_model=list[TagOut])
async def get_tags(db: DbSession, scope: Scope) -> list[TagOut]:
    return [tag_out(t, n) for t, n in await list_tags(db, scope)]


@router.post("/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def add_tag(
    body: TagCreate, db: DbSession, scope: Scope, user: CurrentUser
) -> TagOut:
    _require_admin(scope)
    tag = await create_tag(
        db, name=body.name, category_id=body.category_id, color=body.color
    )
    await record(db, user, "tag.create", tag, details=await _tag_fields(db, tag))
    return tag_out(tag)


async def _tag_fields(db: DbSession, tag: Tag) -> dict:
    category = (
        await db.get(TagCategory, tag.category_id) if tag.category_id is not None else None
    )
    return {
        "name": tag.name,
        "category": category.name if category is not None else None,
        "color": tag.color,
    }


async def _admin_tag(db: DbSession, scope: Scope, tag_id: str) -> Tag:
    _require_admin(scope)
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise not_found("Tag")
    return tag


@router.patch("/tags/{tag_id}", response_model=TagOut)
async def patch_tag(
    tag_id: str, body: TagUpdate, db: DbSession, scope: Scope, user: CurrentUser
) -> TagOut:
    tag = await _admin_tag(db, scope, tag_id)
    before = await _tag_fields(db, tag)
    tag = await update_tag(
        db,
        tag,
        name=body.name,
        category_id=body.category_id,
        category_given="category_id" in body.model_fields_set,
        color=body.color,
    )
    if diff := changes(before, await _tag_fields(db, tag)):
        await record(db, user, "tag.update", tag, details=diff)
    return tag_out(tag, await tag_vm_count(db, scope, tag.id))


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tag(
    tag_id: str, db: DbSession, scope: Scope, user: CurrentUser
) -> Response:
    """Delete the tag; it is removed from every VM, the VMs stay."""
    tag = await _admin_tag(db, scope, tag_id)
    await record(db, user, "tag.delete", tag)
    await delete_tag(db, tag)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

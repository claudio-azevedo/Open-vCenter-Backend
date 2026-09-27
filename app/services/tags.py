from __future__ import annotations

import logging
import re

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.errors import ApiError, not_found
from ..ids import new_id
from ..models import (
    DEFAULT_TAG_COLOR,
    TAG_COLORS,
    TAG_NAME_MAX_LENGTH,
    TAG_NAME_PATTERN,
    Tag,
    TagCategory,
    Vm,
    vm_tags,
)
from .rbac import VisibleScope, vm_visible_clause

log = logging.getLogger(__name__)

_NAME_RE = re.compile(TAG_NAME_PATTERN)


def check_tag_name(what: str, name: str) -> str:
    """The trimmed name, or 400 INVALID when it isn't `[A-Za-z0-9_-]{1,64}`."""
    name = name.strip()
    if not name or len(name) > TAG_NAME_MAX_LENGTH or not _NAME_RE.fullmatch(name):
        raise ApiError(
            "INVALID",
            f"{what} names use letters, digits, '_' and '-' only "
            f"(1-{TAG_NAME_MAX_LENGTH} characters, no spaces)",
        )
    return name


def check_tag_color(color: str) -> str:
    """`color` when it is a palette name, else 400 INVALID."""
    if color not in TAG_COLORS:
        raise ApiError("INVALID", f"Tag colors are one of: {', '.join(TAG_COLORS)}")
    return color


def _duplicate(message: str) -> ApiError:
    return ApiError("DUPLICATE", message, status.HTTP_409_CONFLICT)


def _conflict(message: str) -> ApiError:
    return ApiError("TAG_CONFLICT", message, status.HTTP_409_CONFLICT)


# ---- categories ---------------------------------------------------------------

async def list_categories(db: AsyncSession) -> list[TagCategory]:
    stmt = select(TagCategory).order_by(func.lower(TagCategory.name))
    return list((await db.execute(stmt)).scalars().all())


async def _require_unique_category(
    db: AsyncSession, name: str, *, exclude_id: str | None = None
) -> None:
    stmt = select(TagCategory.id).where(func.lower(TagCategory.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(TagCategory.id != exclude_id)
    if await db.scalar(stmt) is not None:
        raise _duplicate(f"A category named '{name}' already exists")


async def create_category(db: AsyncSession, name: str) -> TagCategory:
    name = check_tag_name("Category", name)
    await _require_unique_category(db, name)
    category = TagCategory(id=new_id(), name=name)
    db.add(category)
    await db.flush()
    log.info("created tag category=%s name=%s", category.id, name)
    return category


async def rename_category(db: AsyncSession, category: TagCategory, name: str) -> TagCategory:
    name = check_tag_name("Category", name)
    await _require_unique_category(db, name, exclude_id=category.id)
    category.name = name
    await db.flush()
    return category


async def delete_category(db: AsyncSession, category: TagCategory) -> None:
    """Delete a category together with its tags; the FK cascades remove those tags
    from every VM. The VMs themselves are untouched."""
    await db.delete(category)
    await db.flush()
    log.info("deleted tag category=%s (with its tags)", category.id)


# ---- tags -----------------------------------------------------------------------

async def list_tags(db: AsyncSession, scope: VisibleScope) -> list[tuple[Tag, int]]:
    """Every tag with the number of VMs carrying it that `scope` can see."""
    tags = (
        await db.execute(
            select(Tag)
            .outerjoin(TagCategory, TagCategory.id == Tag.category_id)
            # categorized tags first (by category), then standalone ones
            .order_by(
                TagCategory.name.is_(None),
                func.lower(TagCategory.name),
                func.lower(Tag.name),
            )
        )
    ).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(vm_tags.c.tag_id, func.count())
                .select_from(vm_tags)
                .join(Vm, Vm.id == vm_tags.c.vm_id)
                .where(vm_visible_clause(scope))
                .group_by(vm_tags.c.tag_id)
            )
        ).all()
    )
    return [(t, counts.get(t.id, 0)) for t in tags]


async def tag_vm_count(db: AsyncSession, scope: VisibleScope, tag_id: str) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(vm_tags)
            .join(Vm, Vm.id == vm_tags.c.vm_id)
            .where(vm_tags.c.tag_id == tag_id)
            .where(vm_visible_clause(scope))
        )
        or 0
    )


async def _get_category(db: AsyncSession, category_id: str) -> TagCategory:
    category = await db.get(TagCategory, category_id)
    if category is None:
        raise not_found("Tag category")
    return category


async def _require_unique_tag(
    db: AsyncSession,
    name: str,
    category: TagCategory | None,
    *,
    exclude_id: str | None = None,
) -> None:
    stmt = select(Tag.id).where(func.lower(Tag.name) == name.lower())
    if category is None:
        stmt = stmt.where(Tag.category_id.is_(None))
    else:
        stmt = stmt.where(Tag.category_id == category.id)
    if exclude_id is not None:
        stmt = stmt.where(Tag.id != exclude_id)
    if await db.scalar(stmt) is not None:
        where = f"category '{category.name}'" if category else "the standalone tags"
        raise _duplicate(f"A tag named '{name}' already exists in {where}")


async def create_tag(
    db: AsyncSession,
    *,
    name: str,
    category_id: str | None,
    color: str = DEFAULT_TAG_COLOR,
) -> Tag:
    name = check_tag_name("Tag", name)
    color = check_tag_color(color)
    category = await _get_category(db, category_id) if category_id else None
    await _require_unique_tag(db, name, category)
    tag = Tag(
        id=new_id(),
        name=name,
        category_id=category.id if category else None,
        color=color,
    )
    db.add(tag)
    await db.flush()
    log.info("created tag=%s name=%s category=%s", tag.id, name, tag.category_id)
    return tag


async def update_tag(
    db: AsyncSession,
    tag: Tag,
    *,
    name: str | None,
    category_id: str | None,
    category_given: bool,
    color: str | None = None,
) -> Tag:
    """Rename a tag, recolour it and/or move it to another category (`category_id`
    None = standalone; only applied when `category_given`). Moving into a category
    is refused while a VM carrying this tag already carries another tag of it."""
    new_name = check_tag_name("Tag", name) if name is not None else tag.name
    new_color = check_tag_color(color) if color is not None else tag.color
    new_category_id = category_id if category_given else tag.category_id
    category = await _get_category(db, new_category_id) if new_category_id else None
    await _require_unique_tag(db, new_name, category, exclude_id=tag.id)

    if category is not None and category.id != tag.category_id:
        mine = vm_tags.alias("mine")
        other = vm_tags.alias("other")
        clashes = await db.scalar(
            select(func.count(func.distinct(mine.c.vm_id)))
            .select_from(mine)
            .join(other, other.c.vm_id == mine.c.vm_id)
            .join(Tag, Tag.id == other.c.tag_id)
            .where(mine.c.tag_id == tag.id)
            .where(Tag.category_id == category.id)
            .where(Tag.id != tag.id)
        )
        if clashes:
            raise _conflict(
                f"{clashes} VM(s) with tag '{tag.name}' already have a tag of "
                f"category '{category.name}' - a VM holds one tag per category"
            )

    tag.name = new_name
    tag.color = new_color
    tag.category_id = category.id if category else None
    await db.flush()
    return tag


async def delete_tag(db: AsyncSession, tag: Tag) -> None:
    """Delete a tag; the FK cascade removes it from every VM (the VMs stay)."""
    await db.delete(tag)
    await db.flush()
    log.info("deleted tag=%s", tag.id)


# ---- VM assignments ---------------------------------------------------------------

async def set_vm_tags(db: AsyncSession, vm: Vm, tag_ids: list[str]) -> Vm:
    """Replace the VM's tags with `tag_ids`. Every id must exist, and at most one
    tag per category is allowed. A DB-only change: no agent request, no lock."""
    wanted = list(dict.fromkeys(tag_ids))
    tags = (
        list((await db.execute(select(Tag).where(Tag.id.in_(wanted)))).scalars().all())
        if wanted
        else []
    )
    if len(tags) != len(wanted):
        raise not_found("Tag")

    by_category: dict[str, list[Tag]] = {}
    for t in tags:
        if t.category_id is not None:
            by_category.setdefault(t.category_id, []).append(t)
    for category_id, group in by_category.items():
        if len(group) > 1:
            category = await _get_category(db, category_id)
            names = ", ".join(sorted(t.name for t in group))
            raise _conflict(
                f"A VM holds one tag per category - pick one of {names} "
                f"(category '{category.name}')"
            )

    vm.tags = tags
    await db.flush()
    log.info("vm=%s tags=%s", vm.id, [t.id for t in tags])
    return vm

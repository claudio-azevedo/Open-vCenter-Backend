from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, String, Table, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base, TimestampMixin

# Tag and category names: letters, digits, `_` and `-` (no spaces). Enforced by
# services.tags; mirrored by the frontend's Tag Management dialog.
TAG_NAME_PATTERN = r"^[A-Za-z0-9_-]+$"
TAG_NAME_MAX_LENGTH = 64

# A tag's colour is a name from this fixed palette - the frontend maps each one to
# a theme token (`--color-tag-<name>`), so no raw colour value is ever stored.
TAG_COLORS = (
    "gray",
    "red",
    "orange",
    "yellow",
    "green",
    "teal",
    "blue",
    "navy",
    "purple",
    "pink",
)
DEFAULT_TAG_COLOR = "gray"


class TagCategory(Base, TimestampMixin):
    """A group of mutually exclusive tags: a VM carries at most one tag of a
    category (enforced by services.tags). Global - not scoped to a cluster or
    host. Names are unique, case-insensitively."""

    __tablename__ = "tag_categories"
    __table_args__ = (
        Index("uq_tag_categories_name", func.lower(text("name")), unique=True),
    )

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(TAG_NAME_MAX_LENGTH), nullable=False)


class Tag(Base, TimestampMixin):
    """A label an operator attaches to VMs. Standalone (``category_id`` null) or
    part of one :class:`TagCategory`. Global, like its category.

    Names are unique (case-insensitively) within a category; standalone tags are
    unique among the standalone tags. Deleting a category deletes its tags, and
    deleting a tag removes it from every VM (``vm_tags`` FK cascades)."""

    __tablename__ = "tags"
    __table_args__ = (
        Index(
            "uq_tags_category_name",
            "category_id",
            func.lower(text("name")),
            unique=True,
            postgresql_where=text("category_id IS NOT NULL"),
        ),
        Index(
            "uq_tags_standalone_name",
            func.lower(text("name")),
            unique=True,
            postgresql_where=text("category_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    category_id: Mapped[str | None] = mapped_column(
        UUID_STR,
        ForeignKey("tag_categories.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(TAG_NAME_MAX_LENGTH), nullable=False)
    # one of TAG_COLORS
    color: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DEFAULT_TAG_COLOR
    )


# VM <-> tag assignments. Application-managed like vms.folder_id: the agent never
# sees tags, and inventory refreshes keep the VM row (and so its tags). A VM row
# that is deleted takes its assignments with it.
vm_tags = Table(
    "vm_tags",
    Base.metadata,
    Column(
        "vm_id",
        UUID_STR,
        ForeignKey("vms.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        UUID_STR,
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    ),
)

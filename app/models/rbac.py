from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..ids import new_id
from .base import UUID_STR, Base, TimestampMixin

# Scope levels, coarsest first. A grant at a level implies access to everything
# below it (a cluster grant covers its hosts and their folders/VMs).
SCOPE_LEVELS = ("global", "cluster", "host", "folder")


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # comma-separated role names carried from the IdP token (informational for now)
    roles: Mapped[str | None] = mapped_column(String(512), nullable=True)


class ScopeGrant(Base, TimestampMixin):
    __tablename__ = "scope_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "level", "scope_id", name="user_level_scope"),
    )

    id: Mapped[str] = mapped_column(UUID_STR, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        UUID_STR, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    # null when level == "global"; else a cluster / host / folder id
    scope_id: Mapped[str | None] = mapped_column(UUID_STR, nullable=True)

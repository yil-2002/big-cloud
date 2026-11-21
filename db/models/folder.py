"""``Folders`` table - self referencing tree of directories per user."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    ColumnElement,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base
from db.models.base import TimestampMixin

if TYPE_CHECKING:  # pragma: no cover
    from db.models.file import File
    from db.models.user import User


class Folder(Base, TimestampMixin):
    """A directory inside a user's cloud storage.

    ``parent_id`` is nullable: ``NULL`` means "root". A unique constraint on
    ``(user_id, parent_id, name)`` prevents duplicate sibling names.
    """

    __tablename__ = "folders"
    __table_args__ = (
        # No two sibling folders may share a name inside one user's tree.
        UniqueConstraint("user_id", "parent_id", "name", name="uq_folder_sibling_name"),
        Index("ix_folders_user_parent", "user_id", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: ``NULL`` == root folder of the user.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- relationships -------------------------------------------------------
    owner: Mapped[User] = relationship("User", back_populates="folders", lazy="joined")
    parent: Mapped[Folder | None] = relationship(
        "Folder", remote_side="Folder.id", back_populates="children", lazy="joined"
    )
    children: Mapped[list[Folder]] = relationship(
        "Folder",
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )
    files: Mapped[list[File]] = relationship(
        "File",
        back_populates="folder",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )

    # --- computed properties -------------------------------------------------
    @hybrid_property
    def is_root(self) -> bool:
        """``True`` for top level folders (``parent_id IS NULL``)."""
        return self.parent_id is None

    @is_root.expression
    def is_root(cls) -> ColumnElement[bool]:
        """SQL form: ``parent_id IS NULL``.

        Two rules for SQLAlchemy 2.0 hybrids: the expression function must be
        re-bound to the **same name** (``is_root``), and ``.expression`` already
        binds ``cls`` - never stack ``@classmethod`` on top of it (1.x idiom).
        """
        return cls.parent_id.is_(None)

    def breadcrumb(self, *, separator: str = " / ") -> str:
        """Human readable path built from already loaded ancestors.

        Only walks the objects present in the identity map, so it never triggers
        lazy IO (safe inside async code without ``await``).
        """
        parts: list[str] = [self.name]
        node: Folder | None = self.parent
        seen: set[uuid.UUID] = {self.id}
        while node is not None and node.id not in seen:
            seen.add(node.id)
            parts.append(node.name)
            node = node.parent
        parts.reverse()
        return separator.join(parts)

    async def is_descendant_of(
        self, candidate_parent_id: uuid.UUID, session: AsyncSession
    ) -> bool:
        """Guard against cycles when moving a folder into one of its children.

        Walks *up* from ``candidate_parent_id``; if ``self.id`` is met on the way
        the move would create a loop and must be rejected.
        """
        current: uuid.UUID | None = candidate_parent_id
        visited: set[uuid.UUID] = set()
        while current is not None and current not in visited:
            if current == self.id:
                return True
            visited.add(current)
            current = (
                await session.scalar(select(Folder.parent_id).where(Folder.id == current))
            )
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Folder id={self.id} name={self.name!r} parent_id={self.parent_id}>"

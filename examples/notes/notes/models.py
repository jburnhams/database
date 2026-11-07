"""The example app's records. Change a field here, then `dbtool revision -m "..."` writes the migration."""
from datetime import datetime

from sqlalchemy import ForeignKey, String, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from omarchy_db import make_base

Base = make_base()


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    notes: Mapped[list["Note"]] = relationship(back_populates="author", default_factory=list, repr=False)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    text: Mapped[str]
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"), index=True, default=None, init=False)
    author: Mapped[Author | None] = relationship(back_populates="notes", default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.current_timestamp(), init=False)
    # added after the first release: server_default fills existing rows, so the column can be NOT NULL
    pinned: Mapped[bool] = mapped_column(server_default=false(), default=False)

"""Shared database tooling for omarchy apps.

    from omarchy_db import connect, make_base, session
    from sqlalchemy.orm import Mapped, mapped_column

    Base = make_base()
    class Note(Base):                       # a typed record; Alembic generates the table from it
        __tablename__ = "notes"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        text: Mapped[str]

    engine = connect("notes")                # schema notes of the shared database (socket, peer auth)
    with session(engine) as s:
        s.add(Note(text="hi")); s.commit()

Migrations: `dbtool revision -m "..."` (autogenerate), `dbtool migrate`, `dbtool check`; or in code
`Project.load().upgrade(engine)` at app start. Tests: the pytest plugin's `db_session` / `db_engine` fixtures.
See docs/ADOPTING.md in the database repo.
"""
from .config import url_for, normalise_url, resolve_url
from .guard import LiveDatabaseRefused
from .engine import connect, session
from .models import Base, NAMING_CONVENTION, make_base
from .project import Project
from .migrations import upgrade, downgrade, drift, check_migrations, lint_revisions, stairway

__all__ = ["Base", "LiveDatabaseRefused", "NAMING_CONVENTION", "Project", "check_migrations", "connect", "downgrade", "drift",
           "lint_revisions", "make_base", "normalise_url", "resolve_url", "session", "stairway", "upgrade", "url_for"]

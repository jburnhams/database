"""Throwaway databases for tests and autogenerate.

Postgres: created and dropped through an admin connection, OMARCHY_DB_TEST_ADMIN_URL (default
postgresql+psycopg:///postgres, i.e. you over the socket; needs CREATEDB). In CI point it at the service container,
e.g. postgresql://postgres:postgres@localhost:5432/postgres.  (CI service container, not a secret) gitleaks:allow
"""
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager

from sqlalchemy import create_engine, text

from .config import normalise_url


def admin_url() -> str:
    return normalise_url(os.environ.get("OMARCHY_DB_TEST_ADMIN_URL", "postgresql+psycopg:///postgres"))


def _admin():
    return create_engine(admin_url(), isolation_level="AUTOCOMMIT", pool_size=1)


def pg_available() -> bool:
    try:
        eng = create_engine(admin_url(), pool_size=1)
        with eng.connect() as c: c.execute(text("SELECT 1"))
        eng.dispose(); return True
    except Exception:
        return False


def pg_url(dbname: str) -> str:
    u = create_engine(admin_url()).url.set(database=dbname)
    return u.render_as_string(hide_password=False)


def create_pg(name: str, template: str | None = None) -> str:
    eng = _admin()
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
    eng.dispose()
    return pg_url(name)


def drop_pg(name: str):
    eng = _admin()
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


def unique(prefix: str) -> str:
    return f"{prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


@contextmanager
def scratch_database(postgres: bool, prefix: str = "omdb_scratch"):
    """URL of an empty database that is gone afterwards: a temp SQLite file, or a new Postgres database."""
    if postgres:
        name = unique(prefix)
        url = create_pg(name)
        try: yield url
        finally: drop_pg(name)
    else:
        with tempfile.TemporaryDirectory() as d:
            yield "sqlite:///" + os.path.join(d, "scratch.db")


def sqlite_copy_to_memory(path: str) -> sqlite3.Connection:
    """An in-memory SQLite connection holding a copy of the file (fast per-test clone of a migrated template)."""
    src = sqlite3.connect(path)
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    src.backup(mem); src.close()
    return mem
